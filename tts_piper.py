import logging
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Optional

import numpy as np
import soundfile as sf

from config import (
    ENABLE_TTS,
    ENABLE_LOCAL_TTS_PLAYBACK,
    TTS_PLAYBACK_CMD,
    TTS_OUTPUT_DIR,
)

log = logging.getLogger(__name__)

# Add these to config.py:
#   PIPER_MODEL_PATH = "/home/ron/tts/dobby_final.onnx"
#   PIPER_NOISE_SCALE = 0.3
#   PIPER_NOISE_W = 0.4
#   PIPER_LENGTH_SCALE = 1.0

try:
    from config import PIPER_MODEL_PATH, PIPER_NOISE_SCALE, PIPER_NOISE_W, PIPER_LENGTH_SCALE
except ImportError:
    PIPER_MODEL_PATH = "/home/ron/tts/dobby_final.onnx"
    PIPER_NOISE_SCALE = 0.3
    PIPER_NOISE_W = 0.4
    PIPER_LENGTH_SCALE = 1.0


class PiperTTS:
    def __init__(self):
        self._lock = threading.Lock()

    def synth(self, text: str, model_path=None, noise_scale=None, noise_w=None, length_scale=None):
        if not ENABLE_TTS:
            raise RuntimeError("TTS is disabled by config")
        if not text.strip():
            raise ValueError("Cannot synthesize empty text")

        use_model = model_path or PIPER_MODEL_PATH
        use_noise_scale = noise_scale if noise_scale is not None else PIPER_NOISE_SCALE
        use_noise_w = noise_w if noise_w is not None else PIPER_NOISE_W
        use_length_scale = length_scale if length_scale is not None else PIPER_LENGTH_SCALE

        with self._lock:
            t0 = time.time()
            with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as tmp:
                tmp_path = tmp.name

            try:
                cmd = [
                    'piper',
                    '-m', use_model,
                    '-f', tmp_path,
                    '--noise-scale', str(use_noise_scale),
                    '--noise-w', str(use_noise_w),
                    '--length-scale', str(use_length_scale),
                ]
                proc = subprocess.run(
                    cmd,
                    input=text.encode('utf-8'),
                    capture_output=True,
                    check=True,
                )
                audio, sample_rate = sf.read(tmp_path, dtype='float32')
                elapsed = time.time() - t0
                duration = len(audio) / float(sample_rate)
                log.info(
                    "Piper TTS synthesized text_chars=%d wav=%.2fs infer=%.2fs",
                    len(text), duration, elapsed,
                )
                return audio, sample_rate
            finally:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)

    def synth_to_wav(self, text: str, out_path: str | None = None) -> str:
        audio, sample_rate = self.synth(text)

        if out_path is None:
            ts = int(time.time() * 1000)
            os.makedirs(TTS_OUTPUT_DIR, exist_ok=True)
            out_path = os.path.join(TTS_OUTPUT_DIR, f"tts_{ts}.wav")

        sf.write(out_path, audio, sample_rate)
        duration = len(audio) / float(sample_rate)
        log.info("Piper TTS WAV written file=%s wav=%.2fs", out_path, duration)
        return out_path

    def play_wav(self, wav_path: str) -> bool:
        if not ENABLE_LOCAL_TTS_PLAYBACK:
            return False

        if TTS_PLAYBACK_CMD:
            cmd = TTS_PLAYBACK_CMD.format(wav=wav_path)
            args = shlex.split(cmd)
        else:
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
            log.info("Piper TTS playback completed in %.2fs", time.time() - t0)
            return True
        except Exception as e:
            log.warning(f"Piper TTS playback failed: {e}")
            return False


_tts_singleton: Optional[PiperTTS] = None


def get_tts() -> PiperTTS:
    global _tts_singleton
    if _tts_singleton is None:
        _tts_singleton = PiperTTS()
    return _tts_singleton
