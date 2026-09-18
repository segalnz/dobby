# Operation and configuration

## Services and model selection

The supported layout is system units `assistant.service` and `whisper-server.service`,
running as the installation user. OpenClaw remains its own user service,
`openclaw-gateway.service`. `assistantctl.sh` uses the system unit; privileged service
operations use sudo, while status/log commands are read-only.

Both Whisper server and CLI fallback read **`WHISPER_MODEL` in `config.py`**.
It defaults to `whisper/models/ggml-small.en.bin`, matching the running deployment
at the start of this work. Change this single setting, provision the matching asset,
then restart both services. Old `WHISPER_MODEL_PATH` shell overrides are no longer used.
`run_whisper_server.py --print-command` shows the effective launch arguments.

Configuration paths normally resolve relative to the checkout. `ASSISTANT_STATE_DIR`
can relocate writable state, but must be consistent for the service and CLI clients.
`credentials.py` supplies private values; missing optional fields fall back individually,
and syntax/import failures are visible instead of silently discarding all credentials.

## Response refresh

| Setting in `config.py` | Default | Meaning |
| --- | --- | --- |
| `RESPONSE_REFRESH_ENABLED` | `True` | Enable background cloud refresh. |
| `RESPONSE_REFRESH_MIN_SECONDS` | `1800` | Minimum interval. |
| `RESPONSE_REFRESH_MAX_SECONDS` | `3600` | Maximum interval; set equal to minimum for a fixed period. |
| `RESPONSE_REFRESH_TIMEOUT_SECONDS` | `120` | Maximum cloud request timeout. |
| `RESPONSE_REFRESH_MAX_TOKENS` | `1600` | Token budget for generated JSON. |
| `RESPONSE_CACHE_DIR` | `state/responses/` | Current and previous generated responses. |
| `RESPONSE_SEED_PATH` | `defaults/dobby_responses.json` | Versioned initial/fallback templates. |

The first refresh happens after the configured delay; local commands never wait for
refresh. A text-only cloud call uses `CLOUD_LLM_BASE_URL`, `CLOUD_LLM_MODEL`, and
`CLOUD_LLM_API_KEY` (from `DEEPSEEK_API_KEY` in credentials). It requests JSON output
without tools. Unlike a full gateway agent run, it cannot create stray refresh files
through shell tools. OpenClaw remains the agent path for broader spoken requests.
The host validates
categories, string lengths, and `{device}`/`{value}` placeholders, merges up to eight
responses per category, and replaces the cache atomically.

There are exactly two generated cache slots: `current.json` and `previous.json`.
Failure leaves the known-good cache intact; invalid current data falls back to previous,
then the seed. Historical batches were removed after Git snapshots. Legacy root
`dobby_responses.json` and `.bak` aliases point to these slots for compatibility with
older processes/tools; they are not additional stored versions. The new code does not
create root-level batches. Third-party producers must also follow this cache contract.

Settings are loaded at process startup; restart the assistant after changing them.

## Action acknowledgment and failure reporting

`DEVICE_ACKNOWLEDGMENT_ORDER` accepts:

- `before` (default): speak the existing acknowledgment, then publish the immediate action.
  If publication fails or cannot be confirmed, speak a failure response as well.
- `after`: publish first, then speak either the normal response or a failure response.

`MQTT_PUBLISH_TIMEOUT_SECONDS` defaults to five seconds. Success means Paho completed
publication under the action's configured QoS. At QoS 0 this means transport delivery,
not a broker/device acknowledgment; even higher QoS does not prove physical device state.
The assistant says it could not confirm sending when the result is uncertain. No
blind retry is performed, since repeating a toggle or other action may be undesirable.

A working TTS engine, speaker, and network are required for audible responses. Errors
are also logged. Empty/noise utterances retain their existing intentional filtering.

## Durable automation

`state/automation.sqlite3` stores delayed actions, sensor conditions, job outcomes,
and undelivered failure/recovery reports. Jobs retain device, action, and level value.
SQLite transactions claim each one-shot job before publication; reconnect restores
pending sensor subscriptions. Sensor-triggered actions execute outside the MQTT network
callback so waiting for publication cannot deadlock that callback.

`AUTOMATION_OVERDUE_POLICY` controls overdue timers after restart:

- `skip` (prepared default): skip past-due timers and report them; keep future timers.
- `run`: attempt overdue timers on restart.

A job found in `running` state after a crash becomes `uncertain`, produces a report,
and is not replayed. Exactly-once physical execution cannot be guaranteed across a
crash/network interruption without support from the device protocol. Expired sensor
rules are not executed and are reported. Live sensor conditions use the configured
24-hour timeout (`SENSOR_CONDITION_TIMEOUT_S`).

Scheduling is acknowledged immediately. Successful completion is recorded silently
by default; set `AUTOMATION_REPORT_SUCCESS=True` for additional completion announcements.
Failure, expiry, skipped, and uncertain outcomes always generate persisted reports.
The voice pipeline delivers those during idle periods, waits for a registered speaker
when TTS is enabled, and marks them delivered after
TTS completes. A crash between speaking and recording delivery can repeat a report.
When TTS is disabled, the report is logged instead.

Back up the database with SQLite's backup API or stop the service before copying its
files; WAL files may hold committed data. Existing pre-upgrade in-memory timers cannot
be recovered and are lost when the old process stops. Conversation context remains
in memory; automation persistence does not imply persistent conversations.

## Music IPC

`jellyfin_cli.py` sends a bounded JSON request over `state/music.sock` to the running
pipeline's music controller. It no longer starts its own player. The socket is mode
`0600`, so the CLI/OpenClaw tool must run as the same Unix user as the assistant.
No TCP listener or unauthenticated music web endpoint is added.

```bash
python3 jellyfin_cli.py status
python3 jellyfin_cli.py search --query "an artist"
python3 jellyfin_cli.py play --query "an album" --type album
python3 jellyfin_cli.py control --action pause
python3 jellyfin_cli.py --json status
```

The assistant must be running the updated code. If the socket is unavailable, the CLI
fails clearly instead of starting a separate controller. `MUSIC_IPC_TIMEOUT_SECONDS`
defaults to 120 seconds; the agent's execution environment needs a compatible timeout.
Existing OpenClaw tool command syntax remains compatible.

## Voices and output types

Multiple voice/output configurations are intentional. The current dispatcher returns
Kokoro for ordinary speech and named notification voices. Dobby uses `KOKORO_VOICE`
(`bm_fable`), `KOKORO_LANG` (`en-gb`), speed `0.70`, and pitch shift `+3` by default.
The pitch implementation uses SciPy resampling; it also changes duration. It does not
use the older proposed SoX effects chain.

Notifications with a named Kokoro voice may override `voice`, `speed`, `lang`, and
`pitch_semitones`. Unspecified named-voice parameters default to speed 1.0, en-gb,
and zero pitch shift. A missing voice or `dobby` uses Dobby's configured settings.
For example, publish this JSON to `assistant/notify/speak`:

```json
{"text":"The laundry is finished.","voice":"bf_lily","speed":1.0,"lang":"en-gb","pitch_semitones":0}
```

Piper code/model settings and legacy Snapcast/SPK1 helpers remain available for their
separate integrations; the current main pipeline sends both music and speech through
SyncStreamer. These alternatives were not moved to `unused/` or silently selected.
Only the superseded v1 specification was archived there.

## Tests and diagnostics

Run offline tests from the project directory:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -v
```

They use temporary state, fake devices/player, and simulated cloud failures. They do
not exercise microphones, speakers, MQTT appliances, or paid cloud calls.

`assistantctl.sh stt-test` and `testphrases` are live diagnostics: they set a local
`state/stt-test-mode` flag and restart the system service, then remove the flag and
restart on normal cleanup. They require sudo permission for service management.
If forcibly killed before cleanup, remove the flag and restart `assistant.service`.
Use `ASSISTANT_PYTHON` to select an alternative diagnostic interpreter; the helper
checks the original `~/venvs/assistant` environment and the checkout's `.venv`.
