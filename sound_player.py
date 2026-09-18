import logging
import os

import numpy as np
import soundfile as sf

from config import SOUNDS_DIR

log = logging.getLogger(__name__)

_cache = {}


def load_sound(name):
    if name in _cache:
        return _cache[name]

    wav_path = os.path.join(SOUNDS_DIR, f'{name}.wav')
    if not os.path.exists(wav_path):
        raise FileNotFoundError(
            f'Sound file not found: {wav_path}. '
            f'Place WAV files in the sounds/ directory.'
        )

    audio, sample_rate = sf.read(wav_path, dtype='float32')
    if audio.ndim > 1:
        audio = audio[:, 0]

    _cache[name] = (audio, sample_rate)
    log.info('Loaded sound: %s samples=%d sr=%d', name, len(audio), sample_rate)
    return audio, sample_rate
