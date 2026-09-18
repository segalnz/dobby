"""Shared Kokoro engine; named notification voices are selected by the caller.

The independent Piper module is retained for other integrations.
"""
import logging

log = logging.getLogger(__name__)

_piper = None
_kokoro = None


def get_tts(voice=None):
    """Return Kokoro (Dobby voice) for None/'dobby', Kokoro for named voices."""
    global _piper, _kokoro

    if voice is None or voice == "dobby":
        if _kokoro is None:
            from tts_kokoro import KokoroTTS
            _kokoro = KokoroTTS()
        return _kokoro

    if _kokoro is None:
        from tts_kokoro import KokoroTTS
        _kokoro = KokoroTTS()
    return _kokoro
