# Spomena ESP32-S3 Smart Assistant Node

Firmware for an ESP32-S3 audio front-end node used by the Spomena local assistant stack.

## Overview

This firmware runs on an ESP32-S3-DevKitC-1 (N16R8) and provides:

- 4-microphone I2S capture (dual RX buses)
- 5-channel UDP audio streaming to the host:
  - `mic1`, `mic2`, `mic3`, `mic4`, `beamformed_mono`
- Local wakeword detection (`okay nabu`) using TFLite Micro + ESPHome-style frontend
- OTA firmware update UI with ElegantOTA
- Runtime status API for diagnostics and integration
- UDP control/event protocol for assistant state transitions

## Hardware

- Board: ESP32-S3-DevKitC-1 N16R8
  - 16 MB flash
  - 8 MB PSRAM
- Microphones: 4x INMP441
- Speaker path: PCM5100A over I2S TX + amplifier enable pin

## Runtime Network Contract

Defaults are configured in `src/main.cpp`.

- ESP32 -> host audio: `UDP_TARGET_IP:4000`
- host -> ESP32 control RX: `*:4001`
- ESP32 -> host control/event TX: `UDP_TARGET_IP:4002`
- host -> ESP32 speaker audio RX: `*:4003`

Handled control/event types include:

- `READY`
- `PROCESSING`
- `TTS_START`
- `TTS_END`
- `MUTE`
- `UNMUTE`
- `SIM_WAKE`
- `WAKE_DETECTED`
- `AUDIO_START`
- `AUDIO_END`
- `VAD_END`
- `LISTEN_TIMEOUT`

## UDP Audio Wire Format

The firmware sends raw binary UDP packets (not JSON) to host port `4000`.

Payload format is a packed C-style struct with little-endian fields:

- `uint32_t magic` (`0x53504D31`, ASCII `SPM1`)
- `uint32_t seq` (monotonic packet counter)
- `uint32_t sampleRate` (currently `16000`)
- `uint16_t frames` (currently up to `128`)
- `uint16_t channels` (currently `5`)
- `int16_t samples[frames * channels]` interleaved PCM

Current stream characteristics:

- Encoding: raw PCM `int16`
- Sample rate: `16 kHz`
- Channel count: `5`
- Channel order per frame:
  - `m1`, `m2`, `m3`, `m4`, `beamformed_mono`

Current packet sizing in firmware:

- Header: `16` bytes
- Audio payload: `128 * 5 * 2 = 1280` bytes
- Total datagram size: `1296` bytes

Notes:

- The stream is not mono or stereo; it is 5-channel interleaved PCM.
- Host STT should typically consume channel 5 (`beamformed_mono`) for initial integration.

Magic field byte order details:

- Firmware writes `magic = 0x53504D31`.
- On the wire (little-endian), the first 4 bytes are `31 4D 50 53`.
- Host parsers should treat header integer fields as little-endian.

## UDP Speaker Output Wire Format

The firmware now supports host-to-device UDP speaker playback on port `4003`.

Playback packet format is binary PCM with a packed little-endian header:

- `uint32_t magic` (`0x53504B31`, ASCII `SPK1`)
- `uint32_t seq` (host-generated sequence counter)
- `uint32_t sampleRate` (must be `16000`)
- `uint16_t frames` (number of frames in this packet)
- `uint16_t channels` (`1` mono or `2` stereo)
- `int16_t samples[frames * channels]` interleaved PCM payload

Accepted playback characteristics:

- Encoding: raw PCM `int16`
- Sample rate: `16 kHz` only
- Channels: `1` or `2`
  - `1` channel is duplicated to both L/R outputs
  - `2` channels map directly to L/R

Operational sequence for speech playback:

1. Host sends `TTS_START` control on port `4001`.
2. Host streams `SPK1` packets to port `4003`.
3. Host sends `TTS_END` control on port `4001`.

Notes:

- On valid speaker packets, firmware enters `PLAYING` state and enables amplifier output.
- On `TTS_END`, firmware returns to `IDLE` and restores mic bus B for wakeword/capture path.
- Packets with bad magic, unsupported sample rate, invalid channel count, or truncated payload are dropped.

Host-side tone test helper:

- Script: `scripts/udp_tts_tone_test.py`
- Purpose: quick end-to-end validation of host -> ESP speaker playback path.

Example:

```bash
python3 scripts/udp_tts_tone_test.py --esp-ip 192.168.5.66 --duration 3 --freq 660 --poll-status
```

The script automatically sends:

1. `TTS_START` to `4001`
2. `SPK1` PCM packets to `4003`
3. `TTS_END` to `4001`

## Streaming Trigger and Utterance Boundaries

Audio streaming is wakeword-gated, not continuous.

Start behavior:

- Device runs wakeword detection continuously.
- On wakeword detection, ESP32-S3 emits `WAKE_DETECTED` on port `4002`, includes an incremented `utterance_id`, and enables audio streaming.
- On first audio packet of that utterance window, ESP32-S3 emits `AUDIO_START` on port `4002`.

Stop behavior:

- ESP32-S3 stops streaming when control messages indicate transition away from listening (for example `PROCESSING`, `AUDIO_END`, `VAD_END`, `TTS_END`, `READY`).
- When an utterance stream ends after it had started, ESP32-S3 emits `AUDIO_END` on port `4002`.
- If no audio packet started for a wake window, ESP32-S3 emits `LISTEN_TIMEOUT` (instead of `AUDIO_END`) when watchdog release triggers.

Control event payload contract:

- Events on `4002` include `type`, `state`, `uptime_ms`, and `utterance_id`.
- Host should track `utterance_id` and ignore stale/out-of-order events with older IDs.

Host integration implication:

- You can rely on ESP32-S3 signaling for stream start (`WAKE_DETECTED` and `AUDIO_START`).
- End-of-utterance is currently host/control-message-driven, so host VAD or boundary logic is still required unless your pipeline sends explicit end control (`VAD_END` or `AUDIO_END`).

### Host State Machine Example (Port `4002` + Port `4000`)

Use control/events on `4002` to gate stream state, and audio packets on `4000` for PCM payload.

```python
state = "IDLE"
buffer = []

def on_event_4002(event_type: str):
  global state, buffer

  if event_type in ("WAKE_DETECTED", "AUDIO_START"):
    # Start a new utterance window.
    state = "LISTENING"
    buffer = []
    return

  if event_type in ("AUDIO_END", "LISTEN_TIMEOUT", "PROCESSING", "TTS_START", "READY"):
    if state == "LISTENING" and buffer:
      run_whisper_transcribe(buffer)  # Consume channel 5 (beamformed_mono).
    state = "IDLE"
    buffer = []

def on_audio_4000(packet):
  global state, buffer

  if state != "LISTENING":
    return

  # Parse little-endian header; verify magic == 0x53504D31.
  # Deinterleave frames and keep channel index 4 (beamformed_mono) for STT.
  buffer.append(extract_beamformed_channel(packet))

  # Optional host VAD: if silence timeout reached, force close utterance.
  if silence_timeout_reached(buffer):
    send_control_4001({"type": "VAD_END"})
```

Whisper-specific notes:

- Feed Whisper mono PCM derived from channel index `4` (`beamformed_mono`).
- Keep sample rate at `16 kHz` (already native from ESP32-S3 packets).
- If your Whisper binding expects float samples, convert `int16` PCM to `[-1.0, 1.0]`.

Detailed packet parsing for Whisper:

- Header layout (little-endian):
  - bytes `0..3`: `magic` (`0x53504D31`)
  - bytes `4..7`: `seq`
  - bytes `8..11`: `sampleRate`
  - bytes `12..13`: `frames`
  - bytes `14..15`: `channels`
- Audio payload starts at byte `16` and contains interleaved `int16` samples.
- For each frame index `i`, beamformed sample is at flat sample index `i * channels + 4`.

Python example (`struct` + NumPy):

```python
import struct
import numpy as np

MAGIC = 0x53504D31

def parse_beamformed_for_whisper(packet: bytes) -> np.ndarray:
  if len(packet) < 16:
    raise ValueError("packet too small")

  magic, seq, sample_rate, frames, channels = struct.unpack_from("<IIIHH", packet, 0)
  if magic != MAGIC:
    raise ValueError(f"bad magic: 0x{magic:08X}")
  if channels < 5:
    raise ValueError(f"unexpected channels: {channels}")
  if sample_rate != 16000:
    raise ValueError(f"unexpected sample rate: {sample_rate}")

  expected_bytes = 16 + frames * channels * 2
  if len(packet) < expected_bytes:
    raise ValueError("truncated packet")

  pcm_i16 = np.frombuffer(packet, dtype="<i2", count=frames * channels, offset=16)
  pcm_i16 = pcm_i16.reshape((frames, channels))
  beam_i16 = pcm_i16[:, 4]  # channel index 4 = beamformed_mono

  # Whisper stacks often expect float32 mono in [-1.0, 1.0].
  beam_f32 = beam_i16.astype(np.float32) / 32768.0
  return beam_f32
```

Node.js example (`Buffer` + `Float32Array`):

```javascript
const MAGIC = 0x53504d31;

function parseBeamformedForWhisper(buf) {
  if (buf.length < 16) throw new Error("packet too small");

  const magic = buf.readUInt32LE(0);
  const seq = buf.readUInt32LE(4);
  const sampleRate = buf.readUInt32LE(8);
  const frames = buf.readUInt16LE(12);
  const channels = buf.readUInt16LE(14);

  if (magic !== MAGIC) throw new Error(`bad magic: 0x${magic.toString(16)}`);
  if (channels < 5) throw new Error(`unexpected channels: ${channels}`);
  if (sampleRate !== 16000) throw new Error(`unexpected sample rate: ${sampleRate}`);

  const expectedBytes = 16 + frames * channels * 2;
  if (buf.length < expectedBytes) throw new Error("truncated packet");

  const out = new Float32Array(frames);
  let offset = 16;

  for (let i = 0; i < frames; i++) {
    const beamIndex = i * channels + 4;
    const beamOffset = offset + beamIndex * 2;
    const s16 = buf.readInt16LE(beamOffset);
    out[i] = s16 / 32768.0;
  }

  return { seq, sampleRate, samples: out };
}
```

Whisper ingestion tips:

- Accumulate consecutive packet chunks into one utterance buffer before transcription.
- Preserve packet order by `seq`; if a gap is detected, continue but mark the utterance as lossy.
- Run transcription when you close the utterance window (`AUDIO_END` or host VAD timeout).

Failure Modes and Recovery (Host Side):

- Packet loss (`seq` gap):
  - Symptom: current `seq` is greater than `last_seq + 1`.
  - Action: keep utterance open, mark `lossy=true`, continue buffering next packets.
  - Reason: dropping whole utterance is usually worse than partial audio for Whisper.
- Duplicate packet (`seq` repeat):
  - Symptom: current `seq` equals `last_seq`.
  - Action: drop duplicate and continue.
  - Reason: avoids double-audio artifacts and repeated words.
- Out-of-order packet (late arrival):
  - Symptom: current `seq` less than `last_seq`.
  - Action: either drop immediately (simplest) or reorder within a tiny jitter window (for example 2-3 packets).
  - Reason: Whisper tolerates minor gaps better than frequent timeline rewrites.
- Bad header magic:
  - Symptom: `magic != 0x53504D31`.
  - Action: discard packet and increment a protocol-error counter.
  - Reason: protects parser state from corrupted/non-audio datagrams.
- Truncated payload:
  - Symptom: packet length smaller than `16 + frames * channels * 2`.
  - Action: discard packet and continue.
  - Reason: partial interleaved frames will corrupt deinterleave math.
- Missing `AUDIO_END` event:
  - Symptom: listening window stays open with silence only.
  - Action: close by host VAD timeout and send `{"type":"VAD_END"}` to port `4001`.
  - Reason: keeps latency bounded and prevents stuck listening state.
- Stale control ordering (for example `PROCESSING` before last audio packet):
  - Symptom: control event arrives slightly ahead of final UDP audio packet.
  - Action: apply a short grace period (for example 100-200 ms) before hard-closing utterance.
  - Reason: reduces clipping of trailing phonemes.
- Long silence after wake event:
  - Symptom: `WAKE_DETECTED` or `AUDIO_START` occurs but no usable speech follows.
  - Action: enforce max-listen timeout and close without STT if buffered audio is below minimum duration.
  - Reason: avoids sending near-empty clips into Whisper.

Practical behavior:

- If `AUDIO_END` never arrives, host VAD timeout should send `VAD_END` to `4001`.
- If `AUDIO_END` arrives first, host can close immediately and proceed to STT.
- This keeps utterance boundaries deterministic even under packet loss or delayed control events.

Recommended default host thresholds (starting point):

| Parameter | Default | Why |
| --- | --- | --- |
| Endpoint grace after stop event | 150 ms | Captures final trailing phonemes when control arrives before last audio UDP packet. |
| VAD silence timeout | 700 ms | Ends utterance quickly without cutting normal short pauses in speech. |
| Max listen window after wake | 8 s | Prevents stuck listening and oversized buffers on no-speech wakes. |
| Minimum audio to transcribe | 300 ms | Avoids near-empty Whisper calls and low-value false triggers. |
| Reorder jitter window | 2 packets | Handles minor UDP reordering with low added latency. |
| Sequence gap tolerance before reset | 20 packets | If exceeded, close utterance and start fresh to avoid heavily corrupted transcripts. |
| Duplicate packet policy | Drop immediately | Prevents repeated samples and duplicate-word artifacts. |
| Protocol error budget (bad magic/truncated) | 10/min | If exceeded, log warning and recycle UDP socket/parser state. |

Tuning guidance:

- If transcripts clip word endings, increase endpoint grace to `200-250 ms`.
- If latency feels high, reduce VAD silence timeout toward `500-600 ms`.
- If many short false captures occur, increase minimum audio to transcribe toward `400-500 ms`.
- If network quality is poor, increase reorder window to `3-4` packets with caution (latency tradeoff).

Authoritative utterance state machine:

| Host state | ESP state | Trigger |
| --- | --- | --- |
| `IDLE` | `IDLE` | None |
| `LISTENING` | `STREAMING` | `WAKE_DETECTED` from ESP |
| `LISTENING` | `STREAMING` | `AUDIO_START` from ESP (log only) |
| `WAITING` | `PROCESSING` | Host VAD fires, host sends `PROCESSING` |
| `FLUSHING -> STT` | `IDLE` | `AUDIO_END` from ESP (authoritative close) |
| `IDLE` | `IDLE` | Host sends `READY` after STT/LLM/TTS pipeline complete |

Fallback path (watchdog):

| Host state | ESP state | Trigger |
| --- | --- | --- |
| `LISTENING` | `IDLE` | ESP stale-listening watchdog fires and sends `AUDIO_END` (if audio started) or `LISTEN_TIMEOUT` (if no audio started) |
| `FLUSHING -> STT` | `IDLE` | Host receives `AUDIO_END` and follows normal flush/transcribe path |
| `IDLE` | `IDLE` | Host receives `LISTEN_TIMEOUT` and clears listen state without Whisper transcription |

## Dashboard and APIs

When connected to Wi-Fi:

- OTA UI: `http://<device-ip>/update`
- Device dashboard: `http://<device-ip>/`
- Status JSON: `http://<device-ip>/api/status`
- Self-test alias: `http://<device-ip>/api/selftest`

Notes:

- The dashboard is now read-only (status + links), with no manual wakeword/speech test buttons.
- `api/status` remains the primary runtime verification endpoint.

## Build Environment

Primary PlatformIO environment:

- `esp32s3_smartassistant` (default)

See `platformio.ini` for full board/framework flags and scripts.

## Entrypoint and Build Inclusion

This project builds all files under `src/` via the `FILE(GLOB_RECURSE ...)` rule in `src/CMakeLists.txt`.

Important:

- `src/app_main_idf_bridge.cpp` is required in this hybrid Arduino + ESP-IDF setup.
  - It provides `app_main()` and bridges into Arduino by calling `initArduino()`, then `setup()` and `loop()`.
  - Without this file, startup behavior can break or become framework-dependent.
- `src/main.cpp` contains the Arduino runtime (`setup()`/`loop()`), networking, OTA, diagnostics, and audio orchestration.

Because all `src/*.cpp` files are compiled, placeholder/test files left in `src/` can create confusion later and should be moved out.

## IDF Component YAML (`src/idf_component.yml`)

`src/idf_component.yml` is ESP-IDF Component Manager metadata in YAML format.

Current content:

```yaml
dependencies:
  esp-dsp: "==1.7.0"
  esp-nn: "==1.2.1"
  esp-tflite-micro: "==1.3.5"
```

What this means:

- `dependencies:`
  - Top-level YAML map of components required by this component.
- `esp-dsp`, `esp-nn`, `esp-tflite-micro`
  - Component names fetched/resolved by Component Manager.
- `"==x.y.z"`
  - Exact version pin (not a range), chosen for reproducibility and to avoid surprise behavior changes.

## Key `platformio.ini` Settings Explained

`platformio.ini` is the primary build/runtime configuration contract for this firmware.

Critical entries and why they matter:

- `[platformio] build_dir = /tmp/pio-smartassistant/build`
  - Uses a fixed out-of-tree build directory for faster incremental builds and cleaner repository roots.
- `default_envs = esp32s3_smartassistant`
  - Ensures commands run against the intended firmware environment by default.
- `platform = .../platform-espressif32.zip`
  - Pins to a specific pioarduino platform bundle.
- `board = esp32-s3-devkitc1-n16r8`
  - Selects the exact target board profile (flash/PSRAM shape and defaults).
- `framework = arduino, espidf`
  - Enables hybrid mode: Arduino app layer with ESP-IDF capabilities.
- `extra_scripts`
  - `pre:scripts/pio_dynamic_include_fallbacks.py`:
    - Adds fallback include paths for framework-packaged headers used by wakeword/TFLM dependencies.
  - `pre/post:scripts/pre_idf5_embed_certs.py`:
    - Handles certificate embedding workflow around build phases.
- `board_build.partitions = partitions.csv`
  - Forces use of the project partition table; changing this changes flash layout.
- `build_flags`
  - `-DCORE_DEBUG_LEVEL=3`: ESP logging verbosity.
  - `-DARDUINO_USB_MODE=1`, `-DARDUINO_USB_CDC_ON_BOOT=1`: USB CDC behavior for serial console access.
  - `-DELEGANTOTA_USE_ASYNC_WEBSERVER=1`: OTA integration mode.
  - `-DLOCAL_SELF_TEST_MODE=0`: default runtime mode selection.
  - Wakeword tuning:
    - `-DWAKEWORD_PROB_CUTOFF=14`
    - `-DWAKEWORD_SLIDING_WINDOW=3`
    - `-DWAKEWORD_MIN_SLICES=30`

## Quarantine Policy (`redundant/`)

To prevent future maintenance confusion, known-unused/archival files are moved under `redundant/`, which is git-ignored.

Current quarantined examples:

- `redundant/src/ota_smoke_test.cpp`
- `redundant/SESSION_HANDOFF.md`
- `redundant/third_party/`

## Local Setup

Create local credentials header:

```bash
cp include/local_env.example.h include/local_env.h
```

Then edit `include/local_env.h` with your local Wi-Fi credentials.

`include/local_env.h` is git-ignored and should never contain shared/real secrets in commits.

## Build, Flash, Monitor

```bash
platformio run -e esp32s3_smartassistant
platformio run -e esp32s3_smartassistant -t upload --upload-port /dev/ttyACM1
platformio device monitor --port /dev/ttyACM1 --baud 115200
```

If your board enumerates on a different port, replace `/dev/ttyACM1` accordingly.

## Current Wakeword Tuning (Production-Leaning)

From `platformio.ini` build flags:

- `WAKEWORD_PROB_CUTOFF=14`
- `WAKEWORD_SLIDING_WINDOW=3`
- `WAKEWORD_MIN_SLICES=30`

These values are stricter than bring-up defaults and were chosen to reduce false accepts in noisy/continuous speech environments.

## Important Implementation Notes

- Wakeword engine uses internal-RAM-first tensor arena allocation with PSRAM fallback.
- Wakeword model load includes failure suppression to avoid repeated allocation/retry loops.
- Speech activity logic includes hysteresis and beam-aware thresholds to reduce active/quiet flapping.
- `sdkconfig.defaults` is the effective defaults file for IDF CMake usage in this project.

## Common Verification Flow

1. Build succeeds without new warnings/errors.
2. Upload succeeds and board resets cleanly.
3. Fetch `api/status` and confirm expected runtime fields, for example:
   - `assistant_state`
   - `speech.active`
   - `wakeword.detected_recently`
  - `wakeword.prob_cutoff`

## Troubleshooting

- Upload failures:
  - Check USB cable quality and selected serial port.
  - Close any monitor process holding the port.
- Device boots but OTA/status unreachable:
  - Confirm Wi-Fi credentials and AP reachability.
  - Check serial logs for connect timeout and IP assignment.
- Wakeword false accepts still high:
  - Increase `WAKEWORD_PROB_CUTOFF` and/or `WAKEWORD_SLIDING_WINDOW` incrementally.
  - Validate true-positive latency impact after each change.

## Key Files

- `src/main.cpp`: system orchestration, I2S, UDP, OTA, dashboard, runtime state
- `src/wakeword_engine.cpp`: wakeword frontend + streaming inference integration
- `src/wakeword_engine.h`: wakeword engine API
- `platformio.ini`: build environment and runtime tuning flags
- `docs/host-code-handoff.md`: host-side MVP implementation handoff and integration test plan
