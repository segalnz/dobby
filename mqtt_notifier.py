import json
import logging
import re
import threading
from collections import deque

import numpy as np
import paho.mqtt.client as mqtt

from config import (
    ENABLE_MQTT_NOTIFICATIONS,
    MQTT_BROKER_HOST,
    MQTT_BROKER_PORT,
    MQTT_NOTIFICATION_TOPIC,
    MQTT_PASSWORD,
    MQTT_USERNAME,
)
from sound_player import load_sound

log = logging.getLogger(__name__)


class MQTTNotifier:
    def __init__(self, tts, streamer, receiver):
        self._tts = tts
        self._streamer = streamer
        self._receiver = receiver

        self._mqtt = mqtt.Client(client_id='assistant_notification_listener')
        if MQTT_USERNAME:
            self._mqtt.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
        self._mqtt.on_connect = self._on_connect
        self._mqtt.on_message = self._on_message

        self._notification_queue = deque()
        self._busy = False
        self._busy_lock = threading.Lock()
        self._has_message = threading.Event()

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            log.info("MQTT notifier connected to %s:%d", MQTT_BROKER_HOST, MQTT_BROKER_PORT)
            client.subscribe(MQTT_NOTIFICATION_TOPIC)
            log.info("Subscribed to notification topic: %s", MQTT_NOTIFICATION_TOPIC)
        else:
            log.error("MQTT notifier connect failed with rc=%s", rc)

    def _on_message(self, client, userdata, msg):
        topic = msg.topic
        if topic != MQTT_NOTIFICATION_TOPIC:
            return

        try:
            raw = msg.payload.decode('utf-8', errors='replace')
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            log.warning("MQTT notification: invalid JSON on topic %s: %s", topic, e)
            return

        text = payload.get('text')
        if not isinstance(text, str) or not text.strip():
            log.warning("MQTT notification: missing or empty 'text' field, skipping")
            return

        notification = {
            'text': text.strip(),
            'voice': payload.get('voice'),
            'pitch_semitones': payload.get('pitch_semitones'),
            'speed': payload.get('speed'),
            'lang': payload.get('lang'),
        }

        self._notification_queue.append(notification)
        self._has_message.set()
        log.info(
            "MQTT notification queued: text=%r voice=%s pitch=%s",
            notification['text'],
            notification['voice'],
            notification['pitch_semitones'],
        )

    def _parse_segments(self, text):
        segments = []
        last_end = 0
        for match in re.finditer(r'\{\{sound:(\w+)\}\}', text):
            if match.start() > last_end:
                segments.append(('tts', text[last_end:match.start()]))
            segments.append(('sound', match.group(1)))
            last_end = match.end()
        if last_end < len(text):
            segments.append(('tts', text[last_end:]))
        if not segments:
            segments.append(('tts', text))
        return segments

    def _process_queue(self):
        log.info("Notification processing thread started")
        while True:
            self._has_message.wait()

            try:
                notification = self._notification_queue.popleft()
            except IndexError:
                self._has_message.clear()
                continue

            with self._busy_lock:
                self._busy = True

            try:
                # Interrupt music if playing — notification speech takes priority
                try:
                    from jellyfin_music import get_music_controller
                    music = get_music_controller()
                    music.interrupt()
                except Exception:
                    pass

                segments = self._parse_segments(notification['text'])
                self._receiver.send_control('TTS_START')

                for i, (seg_type, seg_value) in enumerate(segments):
                    if i > 0:
                        silence = np.zeros(int(0.15 * 16000), dtype=np.float32)
                        from syncstreamer import get_syncstreamer, TtsSource
                        src = TtsSource(silence, 16000, gain=0.42)
                        get_syncstreamer().play_tts(src)

                    if seg_type == 'sound':
                        try:
                            audio, sample_rate = load_sound(seg_value)
                            from syncstreamer import get_syncstreamer, TtsSource
                            src = TtsSource(audio, sample_rate, gain=0.42)
                            get_syncstreamer().play_tts(src)
                        except FileNotFoundError as e:
                            log.warning("MQTT notification: sound not found: %s", e)
                            continue
                    elif seg_type == 'tts':
                        text = seg_value.strip()
                        if not text:
                            continue
                        try:
                            from tts_dispatcher import get_tts as _get_voice_tts
                            voice = notification.get('voice', 'dobby')
                            tts_engine = _get_voice_tts(voice)
                            if voice not in {None, 'dobby'}:
                                audio, sample_rate = tts_engine.synth(
                                    text=text, voice=voice,
                                    speed=notification.get('speed', 1.0),
                                    lang=notification.get('lang', 'en-gb'),
                                    pitch_semitones=notification.get('pitch_semitones', 0),
                                )
                            else:
                                audio, sample_rate = tts_engine.synth(text)
                            from syncstreamer import get_syncstreamer, TtsSource
                            src = TtsSource(audio, sample_rate, gain=0.42)
                            get_syncstreamer().play_tts(src)
                        except ValueError as e:
                            log.warning("MQTT notification: invalid voice or parameter: %s", e)

                self._receiver.send_control('TTS_END')
            except Exception as e:
                log.error("MQTT notification: TTS/stream error: %s", e)
                self._receiver.send_control('TTS_END')
            finally:
                with self._busy_lock:
                    self._busy = False

                # Resume music if it was interrupted
                try:
                    from jellyfin_music import get_music_controller
                    music = get_music_controller()
                    music.resume_after_interrupt()
                except Exception:
                    pass

                if not self._notification_queue:
                    self._has_message.clear()

    def start(self):
        if not ENABLE_MQTT_NOTIFICATIONS:
            log.info("MQTT notifications disabled by config")
            return

        try:
            self._mqtt.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT, 60)
            self._mqtt.loop_start()
        except Exception as e:
            log.error("MQTT notifier: failed to connect: %s", e)

        thread = threading.Thread(target=self._process_queue, daemon=True)
        thread.start()
        log.info("MQTT notifier started")
