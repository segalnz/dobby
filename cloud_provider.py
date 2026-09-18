import json
import logging
import re
import urllib.error
import urllib.request
from typing import Callable, Iterator

from cloud_llm import CloudLLMClient
from config import (
    CLOUD_PROVIDER,
    CLOUD_PROVIDER_FALLBACK,
    OPENCLAW_AUTH_TOKEN,
    OPENCLAW_BASE_URL,
    OPENCLAW_CHAT_PATH,
    OPENCLAW_TIMEOUT_S,
)

log = logging.getLogger(__name__)

# Sentence-ending punctuation used to split streaming text for TTS.
_SENTENCE_END = re.compile(r'(?<=[.!?])\s+')


def speak_fallback(text, spoken, callback):
    """Speak only sentences not already delivered before a stream failed."""
    def key(sentence):
        return re.sub(r"[^\w]+", " ", sentence).strip().casefold()
    delivered = {key(sentence) for sentence in spoken}
    for sentence in _SENTENCE_END.split(text.strip()):
        if sentence and key(sentence) not in delivered:
            callback(sentence)
            delivered.add(key(sentence))


class DeepSeekCloudProvider:
    def __init__(self):
        self._client = CloudLLMClient()

    def reply(self, user_text: str, history: list[dict[str, str]]) -> str:
        return self._client.reply(user_text, history)


class OpenClawCloudProvider:
    def __init__(self, fallback=None):
        self._url = f"{OPENCLAW_BASE_URL.rstrip('/')}{OPENCLAW_CHAT_PATH}"
        self._timeout = OPENCLAW_TIMEOUT_S
        self._token = OPENCLAW_AUTH_TOKEN
        self._fallback = fallback

    def _sanitize(self, text: str) -> str:
        x = (text or "").replace("```", " ")
        x = re.sub(r"`([^`]*)`", r"\1", x)
        x = re.sub(r"\*\*(.*?)\*\*", r"\1", x)
        x = re.sub(r"\*(.*?)\*", r"\1", x)
        x = re.sub(r"^\s*[-*+]\s+", "", x, flags=re.MULTILINE)
        x = re.sub(r"\s+", " ", x).strip()
        return x

    def _build_request(self, messages: list[dict], stream: bool) -> urllib.request.Request:
        payload = {"model": "openclaw", "messages": messages, "stream": stream}
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return urllib.request.Request(
            self._url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

    def _messages(self, user_text: str, history: list[dict[str, str]]) -> list[dict]:
        messages = [{"role": "system", "content": (
            "You are Dobby, a loyal house elf serving a great wizard. "
            "Be BRIEF: at most 2 short sentences, under 25 words total. "
            "This is ONE-WAY voice: never ask questions, never request clarification, never expect a response. Just acknowledge, confirm, or act. "
            "Speak warmly and deferentially — use 'sir' or 'master'. "
            "To play music, call: python3 ~/assistant/jellyfin_cli.py play --query '<query>' [--type artist/album/track/playlist] "
            "or control: python3 ~/assistant/jellyfin_cli.py control --action pause/stop/next/previous. "
            "If the user asks to play music, USE the jellyfin_cli tool — do not just talk about it."
        )}]
        for turn in history:
            messages.append({"role": turn.get("role", "user"), "content": turn.get("content", "")})
        messages.append({"role": "user", "content": user_text})
        return messages

    def _iter_sse_chunks(self, resp) -> Iterator[str]:
        """Yield content string fragments from an SSE stream."""
        for raw_line in resp:
            line = raw_line.decode("utf-8", errors="replace").rstrip("\n\r")
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
                delta = chunk["choices"][0]["delta"]
                content = delta.get("content")
                if content:
                    yield content
            except (KeyError, IndexError, json.JSONDecodeError):
                continue

    def stream_reply(self, user_text: str, history: list[dict[str, str]],
                     sentence_cb: Callable[[str], None]) -> str:
        """
        Stream the response sentence-by-sentence, calling sentence_cb for each
        complete sentence as tokens arrive. Returns the full response text.
        The first sentence (acknowledgment) arrives within ~1s; tool results follow.
        Falls back to non-streaming reply() on error.
        """
        spoken = []
        original_callback = sentence_cb
        def emit(sentence):
            original_callback(sentence)
            spoken.append(sentence)
        messages = self._messages(user_text, history)
        req = self._build_request(messages, stream=True)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                buffer = ""
                full_text = ""
                for fragment in self._iter_sse_chunks(resp):
                    buffer += fragment
                    full_text += fragment
                    # Flush complete sentences to the callback.
                    parts = _SENTENCE_END.split(buffer)
                    for sentence in parts[:-1]:
                        clean = self._sanitize(sentence)
                        if clean:
                            emit(clean)
                    buffer = parts[-1]  # keep incomplete tail
                # Flush any remaining text.
                if buffer.strip():
                    clean = self._sanitize(buffer)
                    if clean:
                        emit(clean)
            if not full_text.strip():
                emit("Dobby received an empty cloud response, sir.")
            return self._sanitize(full_text) or "Dobby received an empty cloud response, sir."
        except Exception as e:
            log.error("OpenClaw stream error: %s — falling back to non-streaming", e)
            fallback = self.reply(user_text, history)
            speak_fallback(fallback, spoken, original_callback)
            return fallback

    def reply(self, user_text: str, history: list[dict[str, str]]) -> str:
        messages = self._messages(user_text, history)
        req = self._build_request(messages, stream=False)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            try:
                obj = json.loads(raw)
            except Exception:
                return self._sanitize(raw) or "OpenClaw returned an empty response."
            try:
                text = obj["choices"][0]["message"]["content"]
                return self._sanitize(text) or "OpenClaw returned an empty response."
            except (KeyError, IndexError, TypeError):
                pass
            return self._sanitize(self._extract_text(obj)) or "OpenClaw returned an empty response."
        except Exception as e:
            log.error("OpenClaw provider error: %s", e)
            if self._fallback is not None:
                return self._fallback.reply(user_text, history)
            return "Cloud agent is unavailable right now. Please try again."

    def _extract_text(self, body: dict) -> str:
        for key in ("answer", "response", "text", "output"):
            val = body.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        data = body.get("data")
        if isinstance(data, dict):
            for key in ("answer", "response", "text", "output"):
                val = data.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
        return ""


def get_cloud_provider():
    provider = (CLOUD_PROVIDER or "deepseek").strip().lower()
    fallback_name = (CLOUD_PROVIDER_FALLBACK or "deepseek").strip().lower()

    fallback = None
    if fallback_name == "deepseek":
        fallback = DeepSeekCloudProvider()

    if provider == "openclaw":
        log.info("Cloud provider selected: openclaw")
        return OpenClawCloudProvider(fallback=fallback)

    log.info("Cloud provider selected: deepseek")
    return DeepSeekCloudProvider()
