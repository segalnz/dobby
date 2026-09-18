#!/usr/bin/env python3
import argparse
import logging
import socket
import time

import numpy as np

from config import (
    BARGE_IN_ESP32_IPS,
    BARGE_IN_MUTE_PORT,
    SNAPCAST_TCP_HOST,
    SNAPCAST_TCP_PORT,
    SNAPCAST_SAMPLE_RATE,
)

log = logging.getLogger(__name__)

DURATION_BEFORE_S = 4.0
DURATION_MUTE_S = 2.0
DURATION_AFTER_S = 3.0
FREQ_LEFT = 440.0
FREQ_RIGHT = 880.0
AMPLITUDE = 0.3


def _generate_stereo_tone(duration_s, freq_left, freq_right, amplitude, sample_rate):
    samples = int(duration_s * sample_rate)
    t = np.arange(samples, dtype=np.float64) / sample_rate
    left = (np.sin(2 * np.pi * freq_left * t) * amplitude).astype(np.float32)
    right = (np.sin(2 * np.pi * freq_right * t) * amplitude).astype(np.float32)
    stereo = np.column_stack((left, right)).reshape(-1)
    pcm = (np.clip(stereo, -1.0, 1.0) * 32767.0).astype(np.int16)
    return pcm


def run_snapcast_test():
    log.info('=== Snapcast + Barge-in Test ===')

    duration_before = DURATION_BEFORE_S
    duration_mute = DURATION_MUTE_S
    duration_after = DURATION_AFTER_S
    freq_left = FREQ_LEFT
    freq_right = FREQ_RIGHT
    amplitude = AMPLITUDE
    sample_rate = SNAPCAST_SAMPLE_RATE

    pcm = _generate_stereo_tone(
        duration_before + duration_mute + duration_after,
        freq_left, freq_right, amplitude, sample_rate,
    )
    chunk_size = sample_rate // 10
    before_chunks = int(duration_before * sample_rate / chunk_size)
    mute_chunks = int(duration_mute * sample_rate / chunk_size)

    tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp_sock.settimeout(5)
    tcp_sock.connect((SNAPCAST_TCP_HOST, SNAPCAST_TCP_PORT))
    tcp_sock.settimeout(None)
    mute_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    mute_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    log.info('Connected to snapserver at %s:%d (L=%.0fHz R=%.0fHz)',
             SNAPCAST_TCP_HOST, SNAPCAST_TCP_PORT, freq_left, freq_right)

    chunk_idx = 0
    t0 = time.time()

    def send_mute(byte_val, label):
        for ip in BARGE_IN_ESP32_IPS:
            try:
                mute_sock.sendto(bytes([byte_val]), (ip, BARGE_IN_MUTE_PORT))
            except Exception as e:
                log.warning('Mute %s to %s failed: %s', label, ip, e)

    for i in range(0, len(pcm), chunk_size):
        chunk = pcm[i:i + chunk_size]
        try:
            tcp_sock.sendall(chunk.tobytes())
        except OSError:
            log.warning('TCP send failed')
            break

        if chunk_idx == before_chunks:
            log.info('Phase: MUTE (sending 0x01)')
            send_mute(0x01, 'duck')

        if chunk_idx == before_chunks + mute_chunks:
            log.info('Phase: RESTORE (sending 0x00)')
            send_mute(0x00, 'restore')

        chunk_idx += 1
        time.sleep(chunk_size / sample_rate)

    tcp_sock.close()
    mute_sock.close()
    elapsed = time.time() - t0
    log.info('Test complete in %.1fs', elapsed)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Snapcast + barge-in test')
    parser.add_argument('--duration-before', type=float, default=DURATION_BEFORE_S)
    parser.add_argument('--mute-duration', type=float, default=DURATION_MUTE_S)
    parser.add_argument('--duration-after', type=float, default=DURATION_AFTER_S)
    args = parser.parse_args()

    DURATION_BEFORE_S = args.duration_before
    DURATION_MUTE_S = args.mute_duration
    DURATION_AFTER_S = args.duration_after

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(name)s %(levelname)s %(message)s',
    )
    run_snapcast_test()
