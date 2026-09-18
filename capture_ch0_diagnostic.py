#!/usr/bin/env python3
"""
One-shot diagnostic: capture a single utterance from the ESP32 and save
ch0 (raw INMP441 m1) and ch4 (beamformed) as separate WAV files.

Usage:
    Stop the pipeline service first, then run:
        python3 capture_ch0_diagnostic.py
    Say the wake word + command when prompted.
    Pull the WAVs from /tmp/ch0_raw.wav and /tmp/ch4_beam.wav to listen.
"""
import socket
import struct
import wave
import math
import sys
import time

# ── constants (match receiver.py / config.py) ──────────────────────────────
UDP_AUDIO_PORT      = 4000
UDP_CONTROL_RX_PORT = 4002
UDP_CONTROL_TX_PORT = 4001
ESP32_IP            = '192.168.5.66'
MAGIC               = 0x53504D31
HEADER_SIZE         = 16
FRAMES_PER_PKT      = 128
CHANNELS            = 5
SAMPLE_RATE         = 16000
OUT_CH0             = '/tmp/ch0_raw.wav'
OUT_CH4             = '/tmp/ch4_beam.wav'

# ── sockets ────────────────────────────────────────────────────────────────
audio_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
audio_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
audio_sock.bind(('0.0.0.0', UDP_AUDIO_PORT))
audio_sock.settimeout(15.0)

ctrl_rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
ctrl_rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
ctrl_rx.bind(('0.0.0.0', UDP_CONTROL_RX_PORT))
ctrl_rx.settimeout(30.0)

ctrl_tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

def send_ctrl(msg):
    ctrl_tx.sendto(msg.encode(), (ESP32_IP, UDP_CONTROL_TX_PORT))
    print(f'  → {msg}')

def rms(samples):
    if not samples:
        return 0.0
    total = sum(s * s for s in samples)
    return math.sqrt(total / len(samples))

# ── flush stale control messages ──────────────────────────────────────────
print('Flushing stale control messages...')
ctrl_rx.settimeout(0.1)
while True:
    try:
        data, _ = ctrl_rx.recvfrom(256)
        msg = data.decode('utf-8', errors='replace').strip()
        print(f'  (flushed) {msg[:60]}')
    except socket.timeout:
        break
print('Flush done.')

# ── wait for wake word ─────────────────────────────────────────────────────
ctrl_rx.settimeout(30.0)
print('\nWaiting for WAKE_DETECTED from ESP32 (say the wake word)...')
while True:
    try:
        data, addr = ctrl_rx.recvfrom(256)
        msg = data.decode('utf-8', errors='replace').strip()
        print(f'  ← {msg[:80]}')
        if 'WAKE_DETECTED' in msg:
            # Do NOT send READY — that tells the ESP32 to go back to idle.
            # Just wait silently for the audio stream.
            break
    except socket.timeout:
        print('  (no control message in 30s — is the service stopped?)')
        sys.exit(1)

# ── collect audio for up to 10s (time-based, not AUDIO_END-based) ─────────
# Using a fixed window avoids being tripped by stale AUDIO_END in socket buf.
CAPTURE_S = 10.0
audio_sock.settimeout(0.2)
ch0_samples = []
ch4_samples = []
start = time.time()
pkts = 0
print(f'Capturing audio for up to {CAPTURE_S:.0f}s — speak your command now...')

consecutive_silent = 0
while time.time() - start < CAPTURE_S:
    try:
        data, _ = audio_sock.recvfrom(4096)
    except socket.timeout:
        consecutive_silent += 1
        if pkts > 0 and consecutive_silent > 20:
            print('  (audio stream ended)')
            break
        continue

    consecutive_silent = 0
    if len(data) < HEADER_SIZE:
        continue
    magic, seq, sr, frames, channels = struct.unpack('<IIIHH', data[:HEADER_SIZE])
    if magic != MAGIC:
        continue

    expected = frames * channels * 2
    if len(data) - HEADER_SIZE < expected:
        continue

    raw = struct.unpack(f'<{frames * channels}h', data[HEADER_SIZE:HEADER_SIZE + expected])
    ch0_samples.extend(raw[0::channels])   # ch0: raw INMP441 m1
    ch4_samples.extend(raw[4::channels])   # ch4: beamformed
    pkts += 1

duration = len(ch0_samples) / SAMPLE_RATE
print(f'\nCaptured {pkts} packets, {duration:.2f}s of audio')

# ── stats ──────────────────────────────────────────────────────────────────
def stats(label, samples):
    r = rms(samples)
    peak = max(abs(s) for s in samples) if samples else 0
    print(f'  {label}: rms={r:.1f}  peak={peak}  ({100*r/32767:.1f}% fs)')
    return r

r0 = stats('ch0 raw    ', ch0_samples)
r4 = stats('ch4 beam   ', ch4_samples)
if r0 > 0 and r4 > 0:
    diff_db = 20 * math.log10(r4 / r0)
    print(f'  beam/raw ratio: {diff_db:+.1f} dB (positive = beam louder)')

# ── write WAVs ─────────────────────────────────────────────────────────────
def write_wav(path, samples):
    import array
    with wave.open(path, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(array.array('h', samples).tobytes())
    print(f'  Saved: {path}')

write_wav(OUT_CH0, ch0_samples)
write_wav(OUT_CH4, ch4_samples)

print(f'\nPull files to your machine for listening:')
print(f'  scp ron@lama:{OUT_CH0} /tmp/ch0_raw.wav')
print(f'  scp ron@lama:{OUT_CH4} /tmp/ch4_beam.wav')
