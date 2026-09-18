import logging
import socket
import threading
import time
from math import gcd

import numpy as np

try:
    from scipy.signal import resample_poly
except Exception:
    resample_poly = None

from config import (
    BARGE_IN_RESTORE_DELAY_MS,
    SNAPCAST_TCP_HOST,
    SNAPCAST_TCP_PORT,
    SNAPCAST_SAMPLE_RATE,
    SNAPCAST_GAIN,
    SNAPCAST_SOFT_CLIP,
    SNAPCAST_FADE_MS,
    SNAPCAST_PRE_SILENCE_MS,
)

from barge_in import get_barge_in

log = logging.getLogger(__name__)


class SnapcastStreamer:
    def __init__(self):
        self._cancel_event = threading.Event()
        self._sock = None
        self._lock = threading.Lock()

    def _connect(self):
        if self._sock is not None:
            return True
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._sock.settimeout(5.0)
            self._sock.connect((SNAPCAST_TCP_HOST, SNAPCAST_TCP_PORT))
            self._sock.settimeout(None)
            log.info('Connected to snapserver at %s:%d', SNAPCAST_TCP_HOST, SNAPCAST_TCP_PORT)
            return True
        except (ConnectionRefusedError, socket.timeout, OSError) as e:
            log.warning('Cannot connect to snapserver at %s:%d: %s',
                        SNAPCAST_TCP_HOST, SNAPCAST_TCP_PORT, e)
            if self._sock:
                self._sock.close()
                self._sock = None
            return False

    def _resample(self, audio, src_rate):
        if len(audio) == 0:
            return np.zeros(0, dtype=np.float32)

        if src_rate == SNAPCAST_SAMPLE_RATE:
            return audio.astype(np.float32)

        n_src = len(audio)
        if resample_poly is not None:
            factor = gcd(int(src_rate), int(SNAPCAST_SAMPLE_RATE))
            up = int(SNAPCAST_SAMPLE_RATE // factor)
            down = int(src_rate // factor)
            return resample_poly(audio.astype(np.float32), up, down).astype(np.float32)

        duration = n_src / float(src_rate)
        n_dst = max(1, int(round(duration * SNAPCAST_SAMPLE_RATE)))
        x_src = np.linspace(0.0, duration, num=n_src, endpoint=False)
        x_dst = np.linspace(0.0, duration, num=n_dst, endpoint=False)
        return np.interp(x_dst, x_src, audio.astype(np.float32)).astype(np.float32)

    def stream(self, audio, sample_rate, channels=1):
        with self._lock:
            if not self._connect():
                return 0.0

            barge_in = get_barge_in()
            if barge_in is not None:
                barge_in.restore()
                if BARGE_IN_RESTORE_DELAY_MS > 0:
                    time.sleep(BARGE_IN_RESTORE_DELAY_MS / 1000.0)

            self._cancel_event.clear()

            if audio.ndim == 2:
                audio = audio.mean(axis=1)
            resampled = self._resample(audio, sample_rate)
            if len(resampled) == 0:
                return 0.0

            fade_samples = int((float(SNAPCAST_FADE_MS) / 1000.0) * SNAPCAST_SAMPLE_RATE)
            if fade_samples > 0:
                fade_samples = min(fade_samples, len(resampled) // 2)
                if fade_samples > 0:
                    env = np.ones(len(resampled), dtype=np.float32)
                    ramp = np.linspace(0.0, 1.0, num=fade_samples, endpoint=False, dtype=np.float32)
                    env[:fade_samples] *= ramp
                    env[-fade_samples:] *= ramp[::-1]
                    resampled = resampled * env

            resampled = resampled.astype(np.float32) * float(SNAPCAST_GAIN)
            if SNAPCAST_SOFT_CLIP:
                resampled = np.tanh(1.35 * resampled) / np.tanh(1.35)
            resampled = np.clip(resampled, -1.0, 1.0)

            pcm = (resampled * 32767.0).astype(np.int16)
            stereo = np.empty(len(pcm) * 2, dtype=np.int16)
            stereo[0::2] = pcm
            stereo[1::2] = pcm

            if SNAPCAST_PRE_SILENCE_MS > 0:
                silence_samples = int(SNAPCAST_PRE_SILENCE_MS / 1000.0 * SNAPCAST_SAMPLE_RATE * 2)
                silence = np.random.default_rng().integers(-1, 2, silence_samples, dtype=np.int16)
                stereo = np.concatenate([silence, stereo])

            chunk_size = 4096
            total_written = 0
            t0 = time.time()

            try:
                for i in range(0, len(stereo), chunk_size):
                    if self._cancel_event.is_set():
                        log.info('Snapcast stream cancelled after %.2fs', time.time() - t0)
                        break
                    chunk = stereo[i:i + chunk_size]
                    self._sock.sendall(chunk.tobytes())
                    total_written += len(chunk)
            except (BrokenPipeError, ConnectionResetError, OSError) as e:
                log.warning('Snapcast TCP connection lost: %s', e)
                self._sock.close()
                self._sock = None

            elapsed = time.time() - t0
            audio_s = len(stereo) / (2 * float(SNAPCAST_SAMPLE_RATE))
            log.info(
                'Snapcast stream audio=%.2fs wall=%.2fs gain=%.2f channels=2 fade_ms=%.1f',
                audio_s, elapsed, SNAPCAST_GAIN, SNAPCAST_FADE_MS,
            )
            return audio_s

    def cancel(self):
        self._cancel_event.set()

    def reset(self):
        self._cancel_event.clear()


_snapcast_singleton = None


def get_snapcast_streamer():
    global _snapcast_singleton
    if _snapcast_singleton is None:
        _snapcast_singleton = SnapcastStreamer()
    return _snapcast_singleton
