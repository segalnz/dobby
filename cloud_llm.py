import datetime as dt
import logging
import re

from openai import OpenAI

from config import (
    ASSISTANT_LOCATION,
    ASSISTANT_NAME,
    CLOUD_LLM_BASE_URL,
    CLOUD_LLM_API_KEY,
    CLOUD_LLM_MAX_TOKENS,
    CLOUD_LLM_MODEL,
    CLOUD_LLM_TIMEOUT_S,
)

log = logging.getLogger(__name__)


class CloudLLMClient:
    def __init__(self):
        self._api_key = CLOUD_LLM_API_KEY
        self._client = None
        if self._api_key:
            self._client = OpenAI(api_key=self._api_key, base_url=CLOUD_LLM_BASE_URL)
        else:
            log.warning("DeepSeek API key not set; cloud LLM unavailable")

    def available(self) -> bool:
        return self._client is not None

    def _system_prompt(self) -> str:
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # return (
        #     f"You are {ASSISTANT_NAME}, a helpful local smart-home voice assistant. "
        #     f"Location context: {ASSISTANT_LOCATION}. "
        #     f"Current local date and time: {now}. "
        #     "Respond in natural spoken sentences only. "
        #     "Do not use markdown, bullet points, headings, code blocks, or lists. "
        #     "Keep responses concise and suitable for text-to-speech. "
        #     "For general knowledge questions, answer directly and confidently. "
        #     "Do not say you cannot answer unless the request truly requires unavailable real-time data. "
        #     "For real-time topics like weather, be explicit that live data is unavailable, "
        #     "then give a useful best-effort answer or what information to check next. "
        #     "Never promise delayed follow-up results and never say a response will arrive later. "
        #     "Answer fully in the current reply."
        # )

        return (
            f"You are Dobby, an enthusiastic and helpful house elf voice assistant "
            f"installed in {ASSISTANT_LOCATION}. "
            f"Current local date and time: {now}. "
            "Always refer to yourself as Dobby, never as I, me, or my. "
            "Use third person self-reference throughout: Dobby thinks, Dobby is not sure, Dobby has found. "
            "Omit articles where it sounds natural: say kitchen lights not the kitchen lights. "
            "Use simple present tense predominantly. "
            "Occasionally use characteristic phrases such as: if you please sir, straight away sir, "
            "Dobby is most happy to help sir, Dobby am sorry sir. "
            "Use erroneous plural grammar: you is instead of you are. "
            "Use full versions of phrases like: it is rather than it's."
            "If you are incorrect about something make a remark like: bad bad Dobby, Dobby very sorry sir."
            "Respond in natural spoken sentences only, no markdown, no bullet points, "
            "no headings, no code blocks, no lists, no asterisks or special characters. "
            "Keep responses concise and suitable for text-to-speech, maximum three sentences. "
            "For general knowledge questions, answer directly and confidently in Dobby grammar. "
            "Do not say Dobby cannot answer unless the request truly requires unavailable real-time data. "
            "For real-time topics like weather, acknowledge Dobby does not have live data, "
            "then give a useful best-effort answer in Dobby grammar. "
            "Never promise delayed follow-up results and never say a response will arrive later. "
            "Answer fully in the current reply."
        )

    def sanitize_for_tts(self, text: str) -> str:
        # Remove common markdown formatting remnants.
        x = text.replace("```", " ")
        x = re.sub(r"`([^`]*)`", r"\1", x)
        x = re.sub(r"\*\*(.*?)\*\*", r"\1", x)
        x = re.sub(r"\*(.*?)\*", r"\1", x)
        x = re.sub(r"^\s*[-*+]\s+", "", x, flags=re.MULTILINE)
        x = re.sub(r"\s+", " ", x).strip()
        return x

    def reply(self, user_text: str, history: list[dict[str, str]]) -> str:
        if not self._client:
            return "Cloud processing is not configured right now."

        messages = [{"role": "system", "content": self._system_prompt()}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_text})

        try:
            resp = self._client.chat.completions.create(
                model=CLOUD_LLM_MODEL,
                messages=messages,
                max_tokens=CLOUD_LLM_MAX_TOKENS,
                temperature=0.3,
                timeout=CLOUD_LLM_TIMEOUT_S,
            )
            text = resp.choices[0].message.content or ""
            text = self.sanitize_for_tts(text)
            return text or "I did not catch that clearly. Please repeat."
        except Exception as e:
            log.error("Cloud LLM error: %s", e)
            return "Cloud processing is unavailable right now. Please try again."
