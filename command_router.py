import json
import logging
import os
import re
from difflib import SequenceMatcher
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

import paho.mqtt.client as mqtt

from config import (
    CLOUD_FALLBACK_MESSAGE,
    CLOUD_MIN_WORDS_FORWARD,
    COMMAND_CONFIDENCE_THRESHOLD,
    DEVICE_REGISTRY_PATH,
    LOCAL_INTENT_MODEL,
    MQTT_BROKER_HOST,
    MQTT_BROKER_PORT,
    MQTT_CLIENT_ID,
    MQTT_PASSWORD,
    MQTT_USERNAME,
    OLLAMA_BASE_URL,
    OLLAMA_KEEP_ALIVE,
    OLLAMA_REQUEST_TIMEOUT_S,
    SENSOR_CONDITION_TIMEOUT_S,
    SENSOR_REGISTRY_PATH,
)

log = logging.getLogger(__name__)


@dataclass
class CommandDecision:
    handled_locally: bool
    forward_to_cloud: bool
    response_text: str
    parsed: dict[str, Any]


class CommandRouter:
    def __init__(self):
        self.device_registry = self._load_json(DEVICE_REGISTRY_PATH).get("devices", {})
        self.sensor_registry = self._load_json(SENSOR_REGISTRY_PATH).get("sensors", {})
        self._device_alias_map = self._build_device_alias_map(self.device_registry)
        self._sensor_alias_map = self._build_sensor_alias_map(self.sensor_registry)

        self._mqtt = mqtt.Client(client_id=MQTT_CLIENT_ID)
        if MQTT_USERNAME:
            self._mqtt.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
        self._mqtt.on_connect = self._on_connect
        self._mqtt.on_message = self._on_message
        self._mqtt.on_disconnect = self._on_disconnect

        self._condition_rules = []
        self._rules_lock = threading.Lock()
        self._subscribed_topics = set()
        self._mqtt_connected = False
        self._deferred_action = None
        from response_cache import ResponseCache
        from config import RESPONSE_CACHE_DIR, RESPONSE_SEED_PATH
        self._response_cache = ResponseCache(RESPONSE_CACHE_DIR, RESPONSE_SEED_PATH)
        self._dobby_responses = self._response_cache.get()

        from automation import AutomationStore, AutomationRunner
        from config import AUTOMATION_DB_PATH, AUTOMATION_OVERDUE_POLICY, AUTOMATION_REPORT_SUCCESS
        self.automations = AutomationStore(AUTOMATION_DB_PATH, AUTOMATION_OVERDUE_POLICY,
                                           report_success=AUTOMATION_REPORT_SUCCESS)
        self._automation_runner = AutomationRunner(self.automations, self._safe_publish)
        self._connect_mqtt()
        self._automation_runner.start()

    def _load_json(self, path: str) -> dict[str, Any]:
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def _norm_name(self, s: str) -> str:
        return " ".join(str(s).strip().lower().replace("_", " ").split())

    def _canonicalize_for_commands(self, text: str) -> str:
        return self._norm_name(text)

    def _resolve_device_from_text(self, text: str) -> str | None:
        t_norm = self._canonicalize_for_commands(text)
        t_compact = t_norm.replace(" ", "")

        # Exact alias contains.
        for alias_norm, device_key in self._device_alias_map.items():
            if alias_norm in t_norm:
                return device_key

        # Compact contains catches forms like kitchenlite vs kitchen light.
        for alias_norm, device_key in self._device_alias_map.items():
            alias_compact = alias_norm.replace(" ", "")
            if len(alias_compact) >= 6 and alias_compact in t_compact:
                return device_key

        # Fuzzy window match over compacted tokens.
        tokens = [x for x in t_norm.split() if x]
        for alias_norm, device_key in self._device_alias_map.items():
            alias_compact = alias_norm.replace(" ", "")
            if len(alias_compact) < 6:
                continue
            max_window = min(4, len(tokens))
            for w in range(1, max_window + 1):
                for i in range(0, len(tokens) - w + 1):
                    frag = "".join(tokens[i:i + w])
                    if SequenceMatcher(None, frag, alias_compact).ratio() >= 0.86:
                        return device_key

        return None

    def _build_device_alias_map(self, devices: dict[str, Any]) -> dict[str, str]:
        # Build a map from normalized invocation phrases to device keys.
        out = {}
        for key, spec in devices.items():
            for inv in spec.get("invocations", []):
                out[self._norm_name(inv)] = key
        return out

    def _build_sensor_alias_map(self, sensors: dict[str, Any]) -> dict[str, str]:
        # For sensors, keep both key and invocations for backward compatibility.
        out = {}
        for key, spec in sensors.items():
            out[self._norm_name(key)] = key
            for inv in spec.get("invocations", spec.get("aliases", [])):
                out[self._norm_name(inv)] = key
        return out

    def _resolve_device_key(self, raw: str | None) -> str | None:
        if raw is None:
            return None
        return self._device_alias_map.get(self._norm_name(raw))

    def _resolve_sensor_key(self, raw: str | None) -> str | None:
        if raw is None:
            return None
        return self._sensor_alias_map.get(self._norm_name(raw))

    def _is_explicit_cloud_request(self, text: str) -> bool:
        t = self._norm_name(text)
        phrases = (
            "send to cloud",
            "pass to cloud",
            "ask cloud",
            "send to the cloud",
            "ask the cloud",
            "process in cloud",
            "cloud process",
            "use cloud",
            "again send to cloud",
            "cloud please",
        )
        return any(p in t for p in phrases)

    def _extract_cloud_query(self, text: str) -> str:
        t = self._norm_name(text)
        # "ask jellyfin to play the beatles" → "play the beatles via jellyfin"
        jellyfin_phrases = (
            r"ask\s+jellyfin\s+to\s+",
            r"jellyfin\s+",
        )
        for pat in jellyfin_phrases:
            m = re.match(rf"^\s*(?:please\s+)?(?:{pat})(.*)$", t)
            if m:
                remainder = m.group(1).strip()
                if remainder:
                    return f"Use the jellyfin_cli.py tool to {remainder}"
                return "What would you like me to do with Jellyfin?"
        patterns = (
            r"^\s*(?:please\s+)?(?:send\s+(?:it\s+)?to\s+(?:the\s+)?cloud)\s*[,.:;-]?\s*(.*)$",
            r"^\s*(?:please\s+)?(?:ask\s+(?:the\s+)?cloud)\s*[,.:;-]?\s*(.*)$",
            r"^\s*(?:please\s+)?(?:pass\s+(?:it\s+)?to\s+(?:the\s+)?cloud)\s*[,.:;-]?\s*(.*)$",
            r"^\s*(?:please\s+)?(?:use\s+cloud)\s*[,.:;-]?\s*(.*)$",
        )
        for pat in patterns:
            m = re.match(pat, t)
            if m:
                return m.group(1).strip()
        return ""

    def _is_command_like(self, text: str) -> bool:
        t = self._canonicalize_for_commands(text)
        if not t:
            return False

        if re.search(
            r"\b(turn|switch|on|off|lights?|dimmer|brightness|sensor|when|after)\b", t
        ):
            return True

        # Fuzzy alias match for minor STT spelling errors.
        for alias in list(self._device_alias_map.keys()) + list(self._sensor_alias_map.keys()):
            if len(alias) < 4:
                continue
            if SequenceMatcher(None, t, alias).ratio() >= 0.82:
                return True
            if alias in t:
                return True

        return False

    def _word_count(self, text: str) -> int:
        t = self._canonicalize_for_commands(text)
        if not t:
            return 0
        return len([w for w in re.split(r"\s+", t) if w])

    def _is_question_like(self, text: str) -> bool:
        """Heuristic: does this look like a question or request for information?"""
        t = text.lower().strip()
        if t.endswith("?"):
            return True
        q_words = ("what", "why", "how", "when", "where", "who", "can you", "could you",
                   "tell me", "explain", "describe", "is it", "are there", "do you")
        return any(t.startswith(w) for w in q_words)

    def _cloud_allowed_for_text(self, text: str) -> bool:
        if self._is_explicit_cloud_request(text):
            return True
        return self._word_count(text) >= CLOUD_MIN_WORDS_FORWARD

    def _has_time_cue(self, text: str) -> bool:
        t = self._canonicalize_for_commands(text)
        return bool(re.search(r"\b(after|in|seconds?|minutes?|mins?|sec|secs)\b", t))

    def _has_condition_cue(self, text: str) -> bool:
        t = self._canonicalize_for_commands(text)
        if re.search(r"\b(when|if|once|unless)\b", t):
            return True
        if any(op in t for op in (" < ", " > ", " <= ", " >= ", " == ")):
            return True
        return False

    def _has_action_cue(self, text: str, action: str) -> bool:
        t = self._canonicalize_for_commands(text)
        if action == "on":
            return bool(re.search(r"\b(on|switch on|turn on|enable|start)\b", t))
        if action == "off":
            return bool(re.search(r"\b(off|switch off|turn off|disable|stop|kill)\b", t))
        if action == "toggle":
            return bool(re.search(r"\b(toggle|switch|flip)\b", t))
        if action == "set_level":
            if re.search(r"\b(brightness|level|dim|dimmer)\b", t):
                return True
            if re.search(r"\b\d+(?:\.\d+)?\s*%\b", t):
                return True
            if re.search(r"\b(to|at)\s+\d+(?:\.\d+)?\b", t):
                return True
            return False
        return True

    def _short_utterance_decision(self, parsed: dict[str, Any]) -> CommandDecision:
        return CommandDecision(
            handled_locally=False,
            forward_to_cloud=False,
            response_text=(
                # "Please repeat with a little more detail, or say send to cloud."
                "Dobby is not sure what you is wanting. Please repeat with more detail, if you would sir."
            ),
            parsed=parsed,
        )

    def _repeat_request_decision(self, parsed: dict[str, Any]) -> CommandDecision:
        return CommandDecision(
            handled_locally=False,
            forward_to_cloud=False,
            # response_text="I think that was a local device command. Please repeat it clearly, or say send to cloud.",
            response_text="Dobby thinks that was a device command. Please repeat it clearly, if you would sir.",

            parsed=parsed,
        )

    def _sensor_topic(self, sensor_spec: dict[str, Any]) -> str | None:
        topic = sensor_spec.get("topic")
        if topic:
            return str(topic)

        cs = sensor_spec.get("chirpstack")
        if isinstance(cs, dict):
            app_id = cs.get("application_id")
            dev_eui = cs.get("device_eui")
            event = cs.get("event", "up")
            if app_id and dev_eui:
                return f"application/{app_id}/device/{dev_eui}/event/{event}"

        return None

    def _failure_response(self, device):
        return f"Dobby could not confirm sending the command for {device.replace('_', ' ')}, sir."

    def _safe_publish(self, device, action, value=None):
        try:
            return self._publish_device_action(device, action, value)
        except Exception:
            log.exception("Device action failed: %s/%s", device, action)
            return False

    def execute_deferred_action(self):
        action = self._deferred_action
        self._deferred_action = None
        if action and not self._safe_publish(**action):
            return self._failure_response(action['device'])
        return None

    def _dobby_say(self, action, device, value=None, device_type=None):
        import random, os, json as _json
        name = device.replace("_", " ")
        self._dobby_responses = self._response_cache.get()
        key = f"{device_type}_{action}" if device_type else action
        tmpl = self._dobby_responses.get(key) or self._dobby_responses.get(action, self._dobby_responses.get("generic", ["Dobby has done it, sir!"]))
        base = random.choice(tmpl).format(device=name, value=value)
        antics = self._dobby_responses.get("antics", [])
        if antics and random.random() < 0.12:
            base += " " + random.choice(antics).format(device=name, value=value)
        return base

    def refresh_dobby_responses(self):
        """Refresh through the cloud; local commands only read the cached results."""
        types = {v.get('type', 'switch') for v in self.device_registry.values()}
        return self._response_cache.refresh(types)

    def _connect_mqtt(self):
        try:
            self._mqtt.connect_async(MQTT_BROKER_HOST, MQTT_BROKER_PORT, 60)
            self._mqtt.loop_start()
        except Exception as e:
            log.error("MQTT connect failed: %s", e)

    def _on_connect(self, client, userdata, flags, rc):
        self._mqtt_connected = rc == 0
        if self._mqtt_connected:
            log.info("MQTT connected to %s:%d", MQTT_BROKER_HOST, MQTT_BROKER_PORT)
            topics = {j['topic'] for j in self.automations.pending() if j.get('topic')}
            for topic in topics:
                client.subscribe(topic)
            self._subscribed_topics = topics
        else:
            log.error("MQTT connect failed with rc=%s", rc)

    def _on_disconnect(self, client, userdata, rc):
        self._mqtt_connected = False

    def _on_message(self, client, userdata, msg):
        raw = msg.payload.decode('utf-8', errors='replace')
        for rule in self.automations.pending():
            if rule.get('topic') != msg.topic:
                continue
            value = self._extract_sensor_value(raw, rule.get('value_expr'), rule.get('json_field'))
            if value is not None and self._eval_condition(value, rule['operator'], rule['target']):
                # Never wait for publication from Paho's network callback thread.
                self._automation_runner.trigger(rule['id'])

    def _extract_sensor_value(self, payload: str, value_expr: str | None, json_field: str | None):
        try:
            # Backward compatibility with prior schema.
            expr = value_expr
            if not expr and json_field:
                expr = f"json:{json_field}"
            if not expr:
                expr = "payload"

            if expr == "payload":
                val = payload
            elif expr == "event":
                # Event topics encode state by message arrival itself.
                val = 1.0
            elif expr.startswith("json:"):
                obj = json.loads(payload)
                path = expr.split(":", 1)[1]
                val = obj
                for part in path.split("."):
                    if isinstance(val, dict):
                        val = val.get(part)
                    else:
                        val = None
                        break
            else:
                return None
            return float(val)
        except Exception:
            return None

    def _eval_condition(self, value: float, op: str, target: float) -> bool:
        if op == "<":
            return value < target
        if op == "<=":
            return value <= target
        if op == ">":
            return value > target
        if op == ">=":
            return value >= target
        if op == "==":
            return value == target
        return False

    def _publish_device_action(self, device: str, action: str, value: float | None = None) -> bool:
        d = self.device_registry.get(device, {})
        a = d.get("actions", {}).get(action)
        if not a:
            log.warning("Unknown device/action: %s/%s", device, action)
            return False

        topic = a.get("topic")
        payload_type = str(a.get("payload_type", "text")).lower()
        payload_raw = a.get("payload", "")
        payload_template = a.get("payload_template")

        if payload_template is not None:
            if value is None:
                log.warning("Action %s/%s requires value but none was provided", device, action)
                return False

            value_out = float(value)
            if action == "set_level" and value_out > 1.0 and value_out <= 100.0:
                # Accept common "percent" style values from model extraction.
                value_out = value_out / 100.0

            vmin = a.get("value_min")
            vmax = a.get("value_max")
            if vmin is not None:
                value_out = max(float(vmin), value_out)
            if vmax is not None:
                value_out = min(float(vmax), value_out)

            decimals = a.get("value_decimals")
            if decimals is not None:
                value_out = round(value_out, int(decimals))

            payload_raw = payload_template.format(value=value_out)

        if payload_type == "json":
            payload = json.dumps(payload_raw)
        else:
            payload = str(payload_raw)

        retain = bool(a.get("retain", False))
        qos = int(a.get("qos", 0))

        from config import MQTT_PUBLISH_TIMEOUT_SECONDS
        if not self._mqtt_connected:
            return False
        result = self._mqtt.publish(topic, payload, qos=qos, retain=retain)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            return False
        result.wait_for_publish(timeout=MQTT_PUBLISH_TIMEOUT_SECONDS)
        ok = result.is_published()
        if ok:
            log.info("MQTT publish topic=%s payload=%s", topic, payload)
        else:
            log.error("MQTT publish failed topic=%s rc=%s", topic, result.rc)
        return ok

    def _schedule_device_action(self, device, action, delay_seconds, value=None):
        return self.automations.add({'device': device, 'action': action, 'value': value},
                                    due=time.time() + delay_seconds)

    def _add_condition_rule(self, device: str, action: str, condition: dict[str, Any], value=None) -> bool:
        sensor = self._resolve_sensor_key(condition.get("sensor"))
        operator = condition.get("operator")
        target = condition.get("value")

        if sensor is None:
            return False

        s = self.sensor_registry.get(sensor, {})
        if not bool(s.get("active", True)):
            log.warning("Sensor '%s' is inactive/unconfigured", sensor)
            return False

        topic = self._sensor_topic(s)
        value_expr = s.get("value_expr")
        json_field = s.get("json_field")  # backward compatibility
        if topic is None:
            return False

        if value_expr == "event":
            if operator is None:
                operator = "=="
            if target is None:
                target = 1.0

        if operator not in ("<", "<=", ">", ">=", "=="):
            return False

        try:
            target_f = float(target)
        except Exception:
            return False

        rule = {
            'device': device, 'action': action, 'value': value,
            'sensor': sensor, 'topic': topic, 'value_expr': value_expr,
            'json_field': json_field, 'operator': operator, 'target': target_f,
        }
        self.automations.add(rule, expires=time.time() + SENSOR_CONDITION_TIMEOUT_S)
        if self._mqtt_connected and topic not in self._subscribed_topics:
            self._mqtt.subscribe(topic)
            self._subscribed_topics.add(topic)
        return True

    def _system_prompt(self) -> str:
        device_names = ", ".join(sorted(self.device_registry.keys()))
        sensor_names = ", ".join(sorted(self.sensor_registry.keys()))

        return (
            "You are a local command parser for home automation. "
            "Output ONLY strict JSON with no markdown. "
            "Schema: {intent, confidence, device, action, value, delay_seconds, condition, forward_to_cloud}. "
            "intent is one of: device_control, general. "
            "action is one of: on, off, toggle, set_level. "
            "Known devices: " + device_names + ". "
            "Known sensors: " + sensor_names + ". "
            "Map paraphrases to actions: 'kill lights' => off, 'switch on' => on. "
            "For brightness commands like 'set kitchen lights to 50 percent', use action='set_level' and value=50. "
            "For time phrases like 'after 5 minutes', set delay_seconds integer. "
            "For sensor condition like 'when lux < 50', set condition object "
            "{sensor, operator, value}. "
            "For event sensors (e.g. gate beam broken uplink), set sensor and operator '==' with value 1. "
            "If command is unclear or not device control, set intent='general' and forward_to_cloud=true."
        )

    def _extract_intent(self, text: str) -> dict[str, Any]:
        payload = {
            "model": LOCAL_INTENT_MODEL,
            "prompt": self._system_prompt() + "\nUser: " + text,
            "format": "json",
            "stream": False,
            "keep_alive": OLLAMA_KEEP_ALIVE,
            "options": {
                "temperature": 0.0,
                "num_predict": 180,
            },
        }
        req = urllib.request.Request(
            f"{OLLAMA_BASE_URL}/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        with urllib.request.urlopen(req, timeout=OLLAMA_REQUEST_TIMEOUT_S) as resp:
            obj = json.loads(resp.read().decode("utf-8", errors="replace"))

        raw = obj.get("response", "{}")
        parsed = json.loads(raw)
        return self._normalize(parsed)

    def _fast_parse(self, text: str) -> dict[str, Any] | None:
        """
        Fast deterministic parser for common local commands.
        Keeps latency low even if local model is cold.
        """
        t = self._canonicalize_for_commands(text)
        if not t:
            return None

        # Resolve device by alias match in free text.
        device = self._resolve_device_from_text(t)
        if device is None:
            return None

        # Optional delay parsing: "after 5 minutes", "in 10 seconds".
        delay_seconds = None
        m = re.search(r"\b(?:after|in)\s+(\d+)\s*(second|seconds|sec|secs|minute|minutes|min|mins)\b", t)
        if m:
            n = int(m.group(1))
            unit = m.group(2)
            delay_seconds = n * 60 if unit.startswith("min") else n

        # Brightness parsing: "50%" or "to 50".
        value = None
        action = None
        m_pct = re.search(r"(\d+(?:\.\d+)?)\s*%", t)
        if m_pct:
            action = "set_level"
            value = float(m_pct.group(1))
        elif any(k in t for k in ("brightness", "dim", "dimmer", "level")):
            m_val = re.search(r"\b(?:to|at)\s+(\d+(?:\.\d+)?)\b", t)
            if m_val:
                action = "set_level"
                value = float(m_val.group(1))

        if action is None:
            off_words = r"\b(off|switch off|turn off|kill|disable)\b"
            on_words  = r"\b(on|switch on|turn on|enable)\b"
            if re.search(off_words, t):
                action = "off"
            elif re.search(on_words, t):
                action = "on"

        if action is None:
            return None

        return {
            "intent": "device_control",
            "confidence": 0.96,
            "device": device,
            "action": action,
            "value": value,
            "delay_seconds": delay_seconds,
            "condition": None,
            "forward_to_cloud": False,
        }

    def _normalize(self, parsed: dict[str, Any]) -> dict[str, Any]:
        out = {
            "intent": str(parsed.get("intent", "general")),
            "confidence": float(parsed.get("confidence", 0.0) or 0.0),
            "device": parsed.get("device"),
            "action": parsed.get("action"),
            "value": parsed.get("value"),
            "delay_seconds": parsed.get("delay_seconds"),
            "condition": parsed.get("condition"),
            "forward_to_cloud": bool(parsed.get("forward_to_cloud", False)),
        }

        if out["device"] is not None:
            out["device"] = str(out["device"]).strip()
        if out["action"] is not None:
            out["action"] = str(out["action"]).strip().lower()

        # Normalize common phrasing variants from local model output.
        action_aliases = {
            "turn_on": "on",
            "switch_on": "on",
            "enable": "on",
            "turn_off": "off",
            "switch_off": "off",
            "disable": "off",
            "dim": "set_level",
            "set_brightness": "set_level",
            "set_level_to": "set_level",
        }
        if out["action"] in action_aliases:
            out["action"] = action_aliases[out["action"]]

        raw_value = out.get("value")
        if raw_value is None:
            out["value"] = None
        else:
            try:
                if isinstance(raw_value, str) and raw_value.strip().endswith("%"):
                    raw_value = raw_value.strip()[:-1]
                out["value"] = float(raw_value)
            except Exception:
                out["value"] = None

        ds = out["delay_seconds"]
        if ds in (None, "", "null"):
            out["delay_seconds"] = None
        else:
            try:
                out["delay_seconds"] = max(0, int(float(ds)))
            except Exception:
                out["delay_seconds"] = None

        cond = out.get("condition")
        if not isinstance(cond, dict):
            out["condition"] = None
        else:
            sensor = cond.get("sensor")
            if sensor is not None:
                resolved = self._resolve_sensor_key(str(sensor).strip().lower())
                cond["sensor"] = resolved if resolved is not None else str(sensor).strip().lower()

            op = cond.get("operator")
            if isinstance(op, str):
                op_clean = op.strip()
                op_map = {"lt": "<", "lte": "<=", "gt": ">", "gte": ">=", "eq": "=="}
                cond["operator"] = op_map.get(op_clean.lower(), op_clean)

            val = cond.get("value")
            if val is not None:
                try:
                    cond["value"] = float(val)
                except Exception:
                    cond["value"] = None

        return out

    def handle_text(self, text: str) -> CommandDecision:
        try:
            return self._handle_text(text)
        except Exception:
            log.exception("Command processing failed")
            self._deferred_action = None
            return CommandDecision(False, False, "Dobby could not complete that request, sir.",
                                   {'intent': 'error'})

    def _handle_text(self, text: str) -> CommandDecision:
        if self._is_explicit_cloud_request(text):
            cloud_query = self._extract_cloud_query(text)
            if not cloud_query:
                return CommandDecision(
                    handled_locally=False,
                    forward_to_cloud=False,
                    # response_text="What would you like me to ask cloud?",
                    response_text="What is it you is wanting Dobby to ask cloud, if you please sir?",
                    parsed={"intent": "general", "confidence": 1.0, "forward_to_cloud": False},
                )
            return CommandDecision(
                handled_locally=False,
                forward_to_cloud=True,
                response_text="",
                parsed={
                    "intent": "general",
                    "confidence": 1.0,
                    "forward_to_cloud": True,
                    "cloud_query": cloud_query,
                },
            )

        # ── music playback (Jellyfin) ─
        try:
            from jellyfin_music import get_music_controller
            music = get_music_controller()
            if music.is_music_command(text):
                result = music.handle_command(text)
                if result is not None:
                    state, response = result
                    log.info("Music command: %s → %s", text, state)
                    return CommandDecision(
                        handled_locally=True,
                        forward_to_cloud=False,
                        response_text=response,
                        parsed={
                            "intent": "music_control",
                            "confidence": 1.0,
                            "music_state": state,
                            "forward_to_cloud": False,
                        },
                    )
        except Exception:
            pass

        fast = self._fast_parse(text)
        if fast is not None:
            parsed = self._normalize(fast)
            log.info("Fast intent parsed: %s", parsed)
        else:
            try:
                parsed = self._extract_intent(text)
                log.info("Local intent parsed: %s", parsed)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as e:
                log.warning("Local intent extraction failed: %s", e)
                parsed_fallback = {"intent": "general", "confidence": 0.0, "forward_to_cloud": True}
                if not self._cloud_allowed_for_text(text):
                    return self._short_utterance_decision(parsed_fallback)
                if self._is_command_like(text):
                    return self._repeat_request_decision(parsed_fallback)
                return CommandDecision(
                    handled_locally=False,
                    forward_to_cloud=True,
                    response_text=CLOUD_FALLBACK_MESSAGE,
                    parsed=parsed_fallback,
                )

        # Guardrail: allow delay/condition only if user text contains cues.
        if parsed.get("delay_seconds") not in (None, 0) and not self._has_time_cue(text):
            parsed["delay_seconds"] = None
        if isinstance(parsed.get("condition"), dict) and not self._has_condition_cue(text):
            parsed["condition"] = None

        action = parsed.get("action")
        if action in ("on", "off", "toggle", "set_level") and not self._has_action_cue(text, action):
            if self._is_command_like(text):
                return self._repeat_request_decision(parsed)
            return self._short_utterance_decision(parsed)

        if parsed["forward_to_cloud"]:
            if not self._cloud_allowed_for_text(text):
                return self._short_utterance_decision(parsed)
            # Don't forward garbage unless it looks like a genuine question/query
            if (parsed.get("confidence", 0) <= 0.5
                and not parsed.get("device")
                and not parsed.get("action")
                and not self._is_question_like(text)):
                log.info("Suppressing low-confidence cloud forward: %s", parsed)
                return self._short_utterance_decision(parsed)
            return CommandDecision(
                handled_locally=False,
                forward_to_cloud=True,
                response_text=CLOUD_FALLBACK_MESSAGE,
                parsed=parsed,
            )

        if parsed["intent"] != "device_control" or parsed["confidence"] < COMMAND_CONFIDENCE_THRESHOLD:
            if not self._cloud_allowed_for_text(text):
                return self._short_utterance_decision(parsed)
            return CommandDecision(
                handled_locally=False,
                forward_to_cloud=True,
                response_text=CLOUD_FALLBACK_MESSAGE,
                parsed=parsed,
            )

        device = self._resolve_device_key(parsed.get("device"))
        parsed["device"] = device
        action = parsed.get("action")
        valid_actions = set(self.device_registry.get(device, {}).get("actions", {}).keys())
        if device not in self.device_registry or action not in valid_actions:
            if not self._cloud_allowed_for_text(text):
                return self._short_utterance_decision(parsed)
            return CommandDecision(
                handled_locally=False,
                forward_to_cloud=True,
                response_text=CLOUD_FALLBACK_MESSAGE,
                parsed=parsed,
            )

        value = parsed.get("value")
        delay_seconds = parsed.get("delay_seconds")
        condition = parsed.get("condition")

        if isinstance(condition, dict):
            ok = self._add_condition_rule(device, action, condition, value)
            if not ok:
                if not self._cloud_allowed_for_text(text):
                    return self._short_utterance_decision(parsed)
                return CommandDecision(
                    handled_locally=False,
                    forward_to_cloud=True,
                    response_text=CLOUD_FALLBACK_MESSAGE,
                    parsed=parsed,
                )
            return CommandDecision(
                handled_locally=True,
                forward_to_cloud=False,
                # response_text=f"Okay. I will turn {device.replace('_', ' ')} {action} when condition is met.",
                response_text=self._dobby_say(action, device),
                parsed=parsed,
            )

        if delay_seconds is not None and delay_seconds > 0:
            self._schedule_device_action(device, action, delay_seconds, value)
            return CommandDecision(
                handled_locally=True,
                forward_to_cloud=False,
                # response_text=f"Okay. I will turn {device.replace('_', ' ')} {action} in {delay_seconds} seconds.",
                response_text=f"Dobby will turn {device.replace('_', ' ')} {action} in {delay_seconds} seconds, sir.",

                parsed=parsed,
            )

        from config import DEVICE_ACKNOWLEDGMENT_ORDER
        if DEVICE_ACKNOWLEDGMENT_ORDER not in ('before', 'after'):
            raise ValueError('DEVICE_ACKNOWLEDGMENT_ORDER must be before or after')
        device_type = self.device_registry[device].get('type', 'switch')
        response = self._dobby_say(action, device, value, device_type)
        if DEVICE_ACKNOWLEDGMENT_ORDER == 'before':
            self._deferred_action = {'device': device, 'action': action, 'value': value}
        elif not self._safe_publish(device, action, value):
            return CommandDecision(False, False, self._failure_response(device), parsed)
        return CommandDecision(True, False, response, parsed)



_router_singleton = None


def get_command_router() -> CommandRouter:
    global _router_singleton
    if _router_singleton is None:
        _router_singleton = CommandRouter()
    return _router_singleton
