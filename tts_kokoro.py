import logging
import os
import shlex
import shutil
import subprocess
import threading
import time
from math import gcd
from typing import Optional

import numpy as np
import soundfile as sf
from kokoro_onnx import Kokoro

try:
    from scipy.signal import resample_poly
except Exception:
    resample_poly = None

from config import (
    KOKORO_PITCH_SHIFT_SEMITONES,
    ENABLE_TTS,
    KOKORO_LANG,
    KOKORO_MODEL_PATH,
    KOKORO_SPEED,
    KOKORO_VOICE,
    KOKORO_VOICES_PATH,
    ENABLE_LOCAL_TTS_PLAYBACK,
    TTS_PLAYBACK_CMD,
    TTS_OUTPUT_DIR,
)

log = logging.getLogger(__name__)


class KokoroTTS:
    def __init__(self):
        self._engine: Optional[Kokoro] = None
        self._lock = threading.Lock()
        self._synth_lock = threading.Lock()

    def _ensure_engine(self):
        if self._engine is not None:
            return
        with self._lock:
            if self._engine is None:
                log.info("Loading Kokoro ONNX model")
                self._engine = Kokoro(
                    model_path=KOKORO_MODEL_PATH,
                    voices_path=KOKORO_VOICES_PATH,
                )
                voices = self._engine.get_voices()
                log.info(
                    "Kokoro ready default_voice=%s voices=%d",
                    KOKORO_VOICE,
                    len(voices),
                )

    def synth(self, text: str, voice=None, speed=None, lang=None, pitch_semitones=None):
        if not ENABLE_TTS:
            raise RuntimeError("TTS is disabled by config")
        if not text.strip():
            raise ValueError("Cannot synthesize empty text")

        self._ensure_engine()
        assert self._engine is not None

        use_voice = voice if voice is not None else KOKORO_VOICE
        use_speed = speed if speed is not None else KOKORO_SPEED
        use_lang = lang if lang is not None else KOKORO_LANG
        use_pitch = pitch_semitones if pitch_semitones is not None else KOKORO_PITCH_SHIFT_SEMITONES

        voices = self._engine.get_voices()
        if use_voice not in voices:
            raise ValueError(
                f"Voice '{use_voice}' not found. "
                f"Available voices: {', '.join(voices)}"
            )

        with self._synth_lock:
            t0 = time.time()
            audio, sample_rate = self._engine.create(
                text=text,
                voice=use_voice,
                speed=use_speed,
                lang=use_lang,
            )
            elapsed = time.time() - t0
            duration = len(audio) / float(sample_rate)
            if use_pitch != 0 and len(audio) > 0:
                if resample_poly is None:
                    raise RuntimeError(
                        "scipy is required for pitch shift. Install with: pip install scipy"
                    )
                ratio = 2 ** (use_pitch / 12)
                ratio_den = 1000
                ratio_num = round(ratio * ratio_den)
                g = gcd(ratio_num, ratio_den)
                up = ratio_den // g
                down = ratio_num // g
                audio = resample_poly(
                    audio.astype(np.float32),
                    up,
                    down,
                ).astype(np.float32)
                log.info(
                    "Pitch shifted by %+d semitones ratio=%.4f up=%d down=%d",
                    use_pitch,
                    ratio,
                    up,
                    down,
                )

            log.info(
                "TTS synthesized voice=%s text_chars=%d wav=%.2fs infer=%.2fs",
                use_voice,
                len(text),
                duration,
                elapsed,
            )
            return audio, sample_rate

    def synth_to_wav(self, text: str, out_path: str | None = None) -> str:
        audio, sample_rate = self.synth(text)

        if out_path is None:
            ts = int(time.time() * 1000)
            os.makedirs(TTS_OUTPUT_DIR, exist_ok=True)
            out_path = os.path.join(TTS_OUTPUT_DIR, f"tts_{ts}.wav")

        sf.write(out_path, audio, sample_rate)
        duration = len(audio) / float(sample_rate)
        log.info("TTS WAV written file=%s wav=%.2fs", out_path, duration)
        return out_path

    def play_wav(self, wav_path: str) -> bool:
        if not ENABLE_LOCAL_TTS_PLAYBACK:
            return False

        if TTS_PLAYBACK_CMD:
            cmd = TTS_PLAYBACK_CMD.format(wav=wav_path)
            args = shlex.split(cmd)
        else:
            # Auto-detect a simple local player.
            if shutil.which('aplay'):
                args = ['aplay', '-q', wav_path]
            elif shutil.which('paplay'):
                args = ['paplay', wav_path]
            elif shutil.which('ffplay'):
                args = ['ffplay', '-nodisp', '-autoexit', '-loglevel', 'error', wav_path]
            else:
                log.info("No local audio player found; keeping WAV only")
                return False

        t0 = time.time()
        try:
            subprocess.run(args, check=True)
            log.info("TTS playback completed in %.2fs", time.time() - t0)
            return True
        except Exception as e:
            log.warning(f"TTS playback failed: {e}")
            return False


_tts_singleton: Optional[KokoroTTS] = None


def get_tts() -> KokoroTTS:
    global _tts_singleton
    if _tts_singleton is None:
        _tts_singleton = KokoroTTS()
    return _tts_singleton
