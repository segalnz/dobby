from collections import deque
from typing import Deque


class ConversationMemory:
    """Rolling chat memory: last N user/assistant messages."""

    def __init__(self, max_messages: int = 12):
        self._messages: Deque[dict[str, str]] = deque(maxlen=max_messages)

    def add_user(self, text: str):
        self._messages.append({"role": "user", "content": text})

    def add_assistant(self, text: str):
        self._messages.append({"role": "assistant", "content": text})

    def get_messages(self) -> list[dict[str, str]]:
        return list(self._messages)
