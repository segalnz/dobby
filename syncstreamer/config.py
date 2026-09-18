from dataclasses import dataclass


@dataclass
class SyncStreamerConfig:
    audio_port: int = 5005
    announce_port: int = 5006
    frames_per_packet: int = 256    # 5.333 ms @ 48 kHz
    sample_rate: int = 48000
    server_lead_us: int = 400_000   # 400 ms lead for client buffering
    client_ttl_sec: float = 90.0    # expire unannounced clients
    ping_interval_s: float = 5.0    # optional health check
