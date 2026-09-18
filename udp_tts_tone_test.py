#!/usr/bin/env python3
"""Send a UDP SPK1 sine-wave test tone to ESP32-S3 speaker playback path.

This validates host->ESP TTS streaming over UDP:
1) send TTS_START control to port 4001
2) stream SPK1 audio packets to port 4003
3) send TTS_END control to port 4001

Example:
  python3 scripts/udp_tts_tone_test.py --esp-ip 192.168.5.66 --duration 3
"""

from __future__ import annotations

import argparse
import math
import socket
import struct
import sys
import time
import urllib.request

SPEAKER_MAGIC = 0x53504B31  # SPK1
DEFAULT_SAMPLE_RATE = 16000
DEFAULT_FRAMES = 128
DEFAULT_CTRL_PORT = 4001
DEFAULT_TTS_PORT = 4003


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send SPK1 UDP sine tone to ESP32-S3 playback path"
    )
    parser.add_argument("--esp-ip", required=True, help="ESP32-S3 IP address")
    parser.add_argument(
        "--duration",
        type=float,
        default=2.5,
        help="Tone duration in seconds (default: 2.5)",
    )
    parser.add_argument(
        "--freq", type=float, default=660.0, help="Tone frequency in Hz (default: 660)"
    )
    parser.add_argument(
        "--amp",
        type=int,
        default=9000,
        help="Tone amplitude int16 peak, 1..30000 (default: 9000)",
    )
    parser.add_argument(
        "--sample-rate",
        type=int,
        default=DEFAULT_SAMPLE_RATE,
        help="PCM sample rate (must be 16000)",
    )
    parser.add_argument(
        "--frames",
        type=int,
        default=DEFAULT_FRAMES,
        help="Frames per packet (default: 128)",
    )
    parser.add_argument(
        "--channels",
        type=int,
        choices=[1, 2],
        default=1,
        help="Output channels in packet payload",
    )
    parser.add_argument(
        "--ctrl-port",
        type=int,
        default=DEFAULT_CTRL_PORT,
        help="Control UDP port (default: 4001)",
    )
    parser.add_argument(
        "--tts-port",
        type=int,
        default=DEFAULT_TTS_PORT,
        help="TTS UDP port (default: 4003)",
    )
    parser.add_argument(
        "--poll-status",
        action="store_true",
        help="Poll /api/status before and after send and print assistant_state",
    )
    parser.add_argument(
        "--start-delay",
        type=float,
        default=0.12,
        help="Delay after TTS_START before sending audio (default: 0.12s)",
    )
    parser.add_argument(
        "--end-grace",
        type=float,
        default=0.35,
        help="Delay after last packet before TTS_END (default: 0.35s)",
    )
    parser.add_argument(
        "--fade-ms",
        type=float,
        default=25.0,
        help="Linear fade-in/out time in milliseconds (default: 25)",
    )
    parser.add_argument(
        "--pre-silence-packets",
        type=int,
        default=6,
        help="Number of silent packets before tone (default: 6)",
    )
    parser.add_argument(
        "--post-silence-packets",
        type=int,
        default=6,
        help="Number of silent packets after tone (default: 6)",
    )
    parser.add_argument(
        "--pace-mode",
        choices=["realtime", "burst"],
        default="realtime",
        help="Packet pacing mode: realtime sleep per frame, or burst send (default: realtime)",
    )
    parser.add_argument(
        "--realtime-factor",
        type=float,
        default=1.0,
        help="Scale realtime sleep interval (0.9 sends ~10%% faster, default: 1.0)",
    )
    return parser.parse_args()


def fetch_assistant_state(ip: str, timeout_s: float = 1.5) -> str:
    try:
        with urllib.request.urlopen(
            f"http://{ip}/api/status", timeout=timeout_s
        ) as resp:
            payload = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:  # pragma: no cover - operational helper
        return f"ERR:{type(exc).__name__}"

    marker = '"assistant_state":"'
    idx = payload.find(marker)
    if idx < 0:
        return "UNKNOWN"
    start = idx + len(marker)
    end = payload.find('"', start)
    if end < 0:
        return "UNKNOWN"
    return payload[start:end]


def send_control(sock: socket.socket, esp_ip: str, port: int, event_type: str) -> None:
    payload = f'{{"type":"{event_type}"}}'.encode("utf-8")
    sock.sendto(payload, (esp_ip, port))


def main() -> int:
    args = parse_args()

    if args.sample_rate != 16000:
        print(
            "error: firmware currently accepts only --sample-rate 16000",
            file=sys.stderr,
        )
        return 2
    if args.frames <= 0:
        print("error: --frames must be > 0", file=sys.stderr)
        return 2
    if not (1 <= args.amp <= 30000):
        print("error: --amp must be in range 1..30000", file=sys.stderr)
        return 2
    if args.duration <= 0:
        print("error: --duration must be > 0", file=sys.stderr)
        return 2
    if args.start_delay < 0 or args.end_grace < 0:
        print("error: --start-delay and --end-grace must be >= 0", file=sys.stderr)
        return 2
    if args.fade_ms < 0:
        print("error: --fade-ms must be >= 0", file=sys.stderr)
        return 2
    if args.pre_silence_packets < 0 or args.post_silence_packets < 0:
        print("error: --pre-silence-packets and --post-silence-packets must be >= 0", file=sys.stderr)
        return 2
    if args.realtime_factor <= 0:
        print("error: --realtime-factor must be > 0", file=sys.stderr)
        return 2

    ctrl = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    aud = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    tone_packets = int((args.duration * args.sample_rate) // args.frames)
    if tone_packets <= 0:
        print("error: duration too short for selected frames", file=sys.stderr)
        return 2

    if args.poll_status:
        print("assistant_state_before=", fetch_assistant_state(args.esp_ip))

    send_control(ctrl, args.esp_ip, args.ctrl_port, "TTS_START")
    if args.start_delay > 0:
        time.sleep(args.start_delay)

    seq = 0
    phase = 0.0
    phase_inc = 2.0 * math.pi * args.freq / args.sample_rate
    total_samples = tone_packets * args.frames
    fade_samples = int((args.fade_ms / 1000.0) * args.sample_rate)
    frame_interval = args.frames / args.sample_rate
    paced_interval = frame_interval * args.realtime_factor

    def maybe_sleep_interval():
        if args.pace_mode == "realtime":
            time.sleep(paced_interval)

    def send_packet(samples_mono):
        nonlocal seq

        if args.channels == 1:
            interleaved = samples_mono
        else:
            interleaved = []
            for s in samples_mono:
                interleaved.append(s)
                interleaved.append(s)

        header = struct.pack(
            "<IIIHH",
            SPEAKER_MAGIC,
            seq,
            args.sample_rate,
            args.frames,
            args.channels,
        )
        payload = struct.pack("<" + ("h" * len(interleaved)), *interleaved)
        aud.sendto(header + payload, (args.esp_ip, args.tts_port))
        seq += 1

    # Prime playback path with short digital silence to reduce startup pops.
    for _ in range(args.pre_silence_packets):
        send_packet([0] * args.frames)
        maybe_sleep_interval()

    start = time.time()
    sent_samples = 0
    for _ in range(tone_packets):
        mono = []
        for _i in range(args.frames):
            env = 1.0
            if fade_samples > 0:
                if sent_samples < fade_samples:
                    env = min(env, sent_samples / float(fade_samples))
                tail = total_samples - sent_samples
                if tail < fade_samples:
                    env = min(env, max(0.0, tail / float(fade_samples)))

            mono.append(int(math.sin(phase) * args.amp * env))
            phase += phase_inc
            if phase >= 2.0 * math.pi:
                phase -= 2.0 * math.pi
            sent_samples += 1
        send_packet(mono)
        maybe_sleep_interval()

    # Keep stream alive with short silence tail to avoid end pops.
    for _ in range(args.post_silence_packets):
        send_packet([0] * args.frames)
        maybe_sleep_interval()

    if args.end_grace > 0:
        time.sleep(args.end_grace)

    send_control(ctrl, args.esp_ip, args.ctrl_port, "TTS_END")

    elapsed = time.time() - start
    print(
        f"sent_packets={seq} elapsed_s={elapsed:.3f} freq_hz={args.freq} amp={args.amp} fade_ms={args.fade_ms} pace={args.pace_mode} rtf={args.realtime_factor}"
    )

    if args.poll_status:
        time.sleep(0.2)
        print("assistant_state_after=", fetch_assistant_state(args.esp_ip))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
