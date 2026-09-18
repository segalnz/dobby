# SyncStreamer v2 — implemented server behavior

This document describes the current source. No SyncStreamer source was changed during
the September 2026 documentation/reliability work.

## Wire format

Audio is unicast to active clients on UDP port 5005. Each source block is 256 stereo
frames at 48 kHz, producing 1,024 bytes of interleaved signed little-endian int16 PCM.
`protocol.pack()` prepends the following 16-byte little-endian header:

| Offset | Bytes | Field |
| --- | --- | --- |
| 0 | 4 | Magic `0xEE15A3D1` |
| 4 | 4 | Sequence; high bit set by sender for TTS |
| 8 | 8 | Presentation timestamp, unsigned microseconds |
| 16 | 1024 | Interleaved stereo PCM |

Total datagram size is 1,040 bytes. Sequence restarts at zero for each `_stream()` call;
TTS uses `sequence | 0x80000000`. There is no separate v1 stream-ID or flags header.
The sender calculates presentation time from wall-clock start plus frame offset and
`server_lead_us` (400,000 by default). Pacing uses a monotonic performance counter at
187.5 packets/second. Firmware must interpret the same protocol, including the TTS bit.

## Sources

`TtsSource(audio, sample_rate, gain=1.0)` accepts NumPy audio, averages channels when
needed, resamples through `numpy.interp`, applies gain/clipping, adds 50 ms of pre-silence,
and duplicates mono into stereo. `JellyfinSource(audio, sample_rate, gain=1.0, channels=1)`
preserves the first two channels or duplicates mono; it also uses interpolation for
resampling. Both pad the final partial block with zeros.

## Discovery and controls

`ClientRegistry` binds UDP 5006. Datagrams whose first ten bytes are `SYNC_HELLO` refresh
the source IP's timestamp. `active_clients()` excludes entries older than the default
90-second TTL. It does not physically remove expired dictionary entries. Stream audio
and controls go to all active IPs; there is no per-room routing in this implementation.

Each stream sends ASCII `STREAM_START`, then audio packets, then `STREAM_STOP` in a
finally block. The current module does **not** send `DUCK_START`, `DUCK_END`, or periodic
PING/PONG health checks. Those descriptions in earlier design notes were not implemented.

## Public behavior

| API | Implemented behavior |
| --- | --- |
| `init_syncstreamer(config=None)` | Creates/replaces the module singleton. Call once during application startup. |
| `get_syncstreamer()` | Returns the singleton, creating one if absent. |
| `play_music(source)` | Blocking paced stream; clears music mute at start. |
| `play_tts(source)` | Blocking TTS stream; suppresses music sends during TTS and restores prior mute state. |
| `stop_music()` | Sets shared stop event; stream loops check it between blocks. |
| `on_mic_open()` / `on_mic_close()` | Set/clear music suppression. |
| `state` | Currently always returns `"idle"`; not a full playback state machine. |
| `send_ready_ping()` | Sends a packet with zero sequence/timestamp and zero PCM; not a distinct stream-ID protocol. |

Methods `active_client_count()` and `current_stream_id()` described in older docs do
not exist on this module. Registry `count()` returns the active client count.

Music suppression skips sending music packets while its source iteration continues;
it is not a sample-accurate pause. Explicit player interruption/resume is handled by
`jellyfin_music.py`. Concurrent streams share a stop event, so this is not a general
independent multistream mixer.

## Configuration boundaries

The main pipeline constructs `SyncStreamerConfig()` with defaults. Root `config.py`
`SYNCSTREAMER_*` values are descriptive legacy settings, not overrides passed into this
instance. The dataclass's announce port, TTL, and lead time are used. Audio port 5005,
48 kHz, and 256-frame blocks are also constants in sender/protocol/source; changing
similarly named dataclass fields alone will not reconfigure the full wire format.
`ping_interval_s` is currently unused.

## Example

```python
import numpy as np
from syncstreamer import init_syncstreamer, TtsSource

streamer = init_syncstreamer()
# Call only after clients have registered; this plays one second of silence.
streamer.play_tts(TtsSource(np.zeros(48000, dtype=np.float32), 48000))
```

The module needs NumPy plus the Python standard library. Use the assistant logs to
check registration and network diagnostics to confirm port 5005 traffic. Compatible
firmware and adequate Wi-Fi delivery are required; the server alone cannot verify
that audio actually emerged from a speaker.
