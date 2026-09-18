import numpy as np
import logging

log = logging.getLogger(__name__)

FRAMES = 256
RATE   = 48000


class AudioSource:
    def generate_blocks(self):
        raise NotImplementedError


class TtsSource(AudioSource):
    """Mono TTS → stereo 256-frame blocks (1024 bytes PCM)."""
    def __init__(self, pcm_float32, sample_rate, gain=1.0):
        self._pcm = self._prepare(pcm_float32, sample_rate, gain)

    def _prepare(self, audio, src_rate, gain):
        if len(audio) == 0:
            return np.zeros(0, dtype="<i2")
        mono = audio if audio.ndim == 1 else audio.mean(axis=1)
        if src_rate != RATE:
            dur = len(mono) / src_rate
            tgt = max(1, int(round(dur * RATE)))
            sx = np.linspace(0, dur, len(mono), endpoint=False)
            tx = np.linspace(0, dur, tgt, endpoint=False)
            mono = np.interp(tx, sx, mono.astype(np.float32))
        mono = np.clip(mono.astype(np.float32) * gain, -1.0, 1.0)
        mono_i16 = (mono * 32767.0).astype("<i2")
        # 50ms pre-silence
        pre = np.zeros(2400, dtype=mono_i16.dtype)
        mono_i16 = np.concatenate([pre, mono_i16])
        # Duplicate mono → interleaved stereo (L,R match client's 1040-byte expectation)
        stereo = np.empty(len(mono_i16) * 2, dtype="<i2")
        stereo[0::2] = mono_i16
        stereo[1::2] = mono_i16
        return stereo

    def generate_blocks(self):
        n = FRAMES * 2  # 512 int16 values = 1024 bytes
        for i in range(0, len(self._pcm) - n + 1, n):
            yield self._pcm[i : i + n].tobytes()
        rem = len(self._pcm) % n
        if rem > 0:
            pad = np.zeros(n - rem, dtype="<i2")
            yield np.concatenate([self._pcm[-rem:], pad]).tobytes()


class JellyfinSource(AudioSource):
    """Stereo music: 256-frame blocks (1024 bytes PCM)."""
    def __init__(self, pcm_float32, sample_rate, gain=1.0, channels=1):
        self._ch = channels
        self._pcm = self._prepare(pcm_float32, sample_rate, gain)

    def _prepare(self, audio, src_rate, gain):
        if len(audio) == 0:
            return np.zeros(0, dtype="<i2")
        ch = audio.shape[1] if audio.ndim > 1 else 1
        if ch >= 2:
            left, right = audio[:, 0], audio[:, 1]
        else:
            left = audio if audio.ndim == 1 else audio[:, 0]
            right = left
        if src_rate != RATE:
            dur = len(left) / src_rate
            tgt = max(1, int(round(dur * RATE)))
            sx = np.linspace(0, dur, len(left), endpoint=False)
            tx = np.linspace(0, dur, tgt, endpoint=False)
            left = np.interp(tx, sx, left.astype(np.float32))
            right = np.interp(tx, sx, right.astype(np.float32))
        left = np.clip(left.astype(np.float32) * gain, -1.0, 1.0)
        right = np.clip(right.astype(np.float32) * gain, -1.0, 1.0)
        li = (left * 32767.0).astype("<i2")
        ri = (right * 32767.0).astype("<i2")
        stereo = np.empty(len(li) * 2, dtype="<i2")
        stereo[0::2] = li
        stereo[1::2] = ri
        return stereo

    def generate_blocks(self):
        n = FRAMES * 2  # 512 int16 values
        for i in range(0, len(self._pcm) - n + 1, n):
            yield self._pcm[i : i + n].tobytes()
        rem = len(self._pcm) % n
        if rem > 0:
            pad = np.zeros(n - rem, dtype="<i2")
            yield np.concatenate([self._pcm[-rem:], pad]).tobytes()
