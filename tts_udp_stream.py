import logging
import socket
import struct
import time
from math import gcd

import numpy as np

try:
    from scipy.signal import resample_poly
except Exception:  # pragma: no cover
    resample_poly = None

from config import (
    ESP32_IP,
    ENABLE_UDP_TTS_STREAM,
    UDP_TTS_AUDIO_PORT,
    UDP_TTS_CHANNELS,
    UDP_TTS_FADE_MS,
    UDP_TTS_FRAMES_PER_PACKET,
    UDP_TTS_GAIN,
    UDP_TTS_POST_SILENCE_PACKETS,
    UDP_TTS_PRE_SILENCE_PACKETS,
    UDP_TTS_REALTIME,
    UDP_TTS_REALTIME_FACTOR,
    UDP_TTS_SAMPLE_RATE,
    UDP_TTS_SOFT_CLIP,
)

log = logging.getLogger(__name__)

SPEAKER_MAGIC = 0x53504B31  # SPK1


class TTSAudioStreamer:
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.target = (ESP32_IP, UDP_TTS_AUDIO_PORT)

    def _resample(self, audio: np.ndarray, src_rate: int) -> np.ndarray:
        if src_rate == UDP_TTS_SAMPLE_RATE:
            return audio.astype(np.float32)

        n_src = len(audio)
        if n_src == 0:
            return np.zeros(0, dtype=np.float32)

        # Prefer polyphase resampling when scipy is available; this preserves
        # speech formants better than linear interpolation and reduces rasp.
        if resample_poly is not None:
            factor = gcd(int(src_rate), int(UDP_TTS_SAMPLE_RATE))
            up = int(UDP_TTS_SAMPLE_RATE // factor)
            down = int(src_rate // factor)
            return resample_poly(audio.astype(np.float32), up, down).astype(np.float32)

        duration = n_src / float(src_rate)
        n_dst = max(1, int(round(duration * UDP_TTS_SAMPLE_RATE)))
        x_src = np.linspace(0.0, duration, num=n_src, endpoint=False)
        x_dst = np.linspace(0.0, duration, num=n_dst, endpoint=False)
        return np.interp(x_dst, x_src, audio.astype(np.float32)).astype(np.float32)

    def stream(self, audio: np.ndarray, sample_rate: int) -> float:
        if not ENABLE_UDP_TTS_STREAM:
            return 0.0

        mono = self._resample(audio, sample_rate)
        if len(mono) == 0:
            return 0.0

        # De-click envelope on production TTS stream.
        fade_samples = int((float(UDP_TTS_FADE_MS) / 1000.0) * UDP_TTS_SAMPLE_RATE)
        if fade_samples > 0:
            fade_samples = min(fade_samples, len(mono) // 2)
            if fade_samples > 0:
                env = np.ones(len(mono), dtype=np.float32)
                ramp = np.linspace(0.0, 1.0, num=fade_samples, endpoint=False, dtype=np.float32)
                env[:fade_samples] *= ramp
                env[-fade_samples:] *= ramp[::-1]
                mono = mono * env

        # Apply output gain before conversion to int16 to avoid clipping in amp/speaker chain.
        pcm = mono.astype(np.float32) * float(UDP_TTS_GAIN)
        if UDP_TTS_SOFT_CLIP:
            # Gentle limiting near rails to reduce harsh clipping artifacts.
            pcm = np.tanh(1.35 * pcm) / np.tanh(1.35)
        pcm = np.clip(pcm, -1.0, 1.0)
        peak = float(np.max(np.abs(pcm)))
        pcm = (pcm * 32767.0).astype(np.int16)

        channels = UDP_TTS_CHANNELS
        if channels == 2:
            payload_samples = np.repeat(pcm[:, None], 2, axis=1).reshape(-1)
        else:
            payload_samples = pcm

        frames_per_packet = UDP_TTS_FRAMES_PER_PACKET
        samples_per_packet = frames_per_packet * channels
        seq = 0

        frame_interval_s = (
            frames_per_packet / float(UDP_TTS_SAMPLE_RATE)
        ) * float(UDP_TTS_REALTIME_FACTOR)
        t0 = time.time()
        t_next = time.monotonic()

        def send_payload_chunk(chunk_i16):
            nonlocal seq, t_next
            header = struct.pack(
                '<IIIHH',
                SPEAKER_MAGIC,
                seq,
                UDP_TTS_SAMPLE_RATE,
                frames_per_packet,
                channels,
            )
            self.sock.sendto(header + chunk_i16.tobytes(), self.target)
            seq += 1
            if UDP_TTS_REALTIME:
                t_next += frame_interval_s
                delay = t_next - time.monotonic()
                if delay > 0:
                    time.sleep(delay)

        # Prime frontend playback path with digital silence.
        if UDP_TTS_PRE_SILENCE_PACKETS > 0:
            zero_chunk = np.zeros(samples_per_packet, dtype=np.int16)
            for _ in range(int(UDP_TTS_PRE_SILENCE_PACKETS)):
                send_payload_chunk(zero_chunk)

        for i in range(0, len(payload_samples), samples_per_packet):
            chunk = payload_samples[i:i + samples_per_packet]
            if len(chunk) < samples_per_packet:
                chunk = np.pad(chunk, (0, samples_per_packet - len(chunk)), mode='constant')
            send_payload_chunk(chunk)

        # Tail silence prevents end-of-speech clicks on some amp/DAC paths.
        if UDP_TTS_POST_SILENCE_PACKETS > 0:
            zero_chunk = np.zeros(samples_per_packet, dtype=np.int16)
            for _ in range(int(UDP_TTS_POST_SILENCE_PACKETS)):
                send_payload_chunk(zero_chunk)

        elapsed = time.time() - t0
        audio_s = len(pcm) / float(UDP_TTS_SAMPLE_RATE)
        log.info(
            'UDP TTS stream target=%s:%d seq=%d audio=%.2fs wall=%.2fs gain=%.2f peak=%.3f rtf=%.2f fade_ms=%.1f pad=%d/%d',
            self.target[0],
            self.target[1],
            seq,
            audio_s,
            elapsed,
            UDP_TTS_GAIN,
            peak,
            UDP_TTS_REALTIME_FACTOR,
            UDP_TTS_FADE_MS,
            UDP_TTS_PRE_SILENCE_PACKETS,
            UDP_TTS_POST_SILENCE_PACKETS,
        )
        return audio_s


_streamer_singleton = None


def get_streamer() -> TTSAudioStreamer:
    global _streamer_singleton
    if _streamer_singleton is None:
        _streamer_singleton = TTSAudioStreamer()
    return _streamer_singleton
