"""Installation settings. Paths resolve relative to this checkout by default."""
import os
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("ASSISTANT_STATE_DIR", str(PROJECT_DIR / "state")))
STT_TEST_MODE_FILE = STATE_DIR / "stt-test-mode"

try:
    import credentials as _credentials
except ModuleNotFoundError as exc:
    if exc.name != "credentials":
        raise
    _credentials = None

# Missing optional fields fall back individually; malformed configuration fails visibly.
def _credential(name, default):
    return getattr(_credentials, name, default)

CRED_DEEPSEEK_API_KEY = _credential('DEEPSEEK_API_KEY', '')
CRED_ESP32_IP = _credential('ESP32_IP', '192.168.5.66')
CRED_HOST_IP = _credential('HOST_IP', '192.168.5.75')
CRED_WHISPER_SERVER_HOST = _credential('WHISPER_SERVER_HOST', '127.0.0.1')
CRED_WHISPER_SERVER_PORT = _credential('WHISPER_SERVER_PORT', 8081)
CRED_OLLAMA_BASE_URL = _credential('OLLAMA_BASE_URL', 'http://127.0.0.1:11434')
CRED_MQTT_BROKER_HOST = _credential('MQTT_BROKER_HOST', '127.0.0.1')
CRED_MQTT_BROKER_PORT = _credential('MQTT_BROKER_PORT', 1883)
CRED_MQTT_USERNAME = _credential('MQTT_USERNAME', '')
CRED_MQTT_PASSWORD = _credential('MQTT_PASSWORD', '')
CRED_OPENCLAW_BASE_URL = _credential('OPENCLAW_BASE_URL', 'http://127.0.0.1:18789')
CRED_OPENCLAW_AUTH_TOKEN = _credential('OPENCLAW_AUTH_TOKEN', '')
CRED_JELLYFIN_API_KEY = _credential('JELLYFIN_API_KEY', '')

ESP32_IP           = CRED_ESP32_IP
HOST_IP            = CRED_HOST_IP

UDP_AUDIO_PORT       = 4000   # lama receives audio
UDP_CONTROL_RX_PORT  = 4002   # lama receives control (ESP sends to 4002)
UDP_CONTROL_TX_PORT  = 4001   # lama sends control (ESP listens on 4001)

WHISPER_BIN = str(PROJECT_DIR / 'whisper/build/bin/whisper-cli')
# Single model setting used by BOTH the persistent server and CLI fallback.
# Matches the model observed in the running service before this change.
WHISPER_MODEL = str(PROJECT_DIR / 'whisper/models/ggml-small.en.bin')
SAMPLE_RATE        = 16000

# Persistent STT server settings (recommended for lower latency)
USE_WHISPER_SERVER = True
WHISPER_SERVER_BIN = str(PROJECT_DIR / 'whisper/build/bin/whisper-server')
WHISPER_SERVER_HOST = CRED_WHISPER_SERVER_HOST
WHISPER_SERVER_PORT = CRED_WHISPER_SERVER_PORT

# Whisper decode tuning
WHISPER_THREADS = 8
WHISPER_AUDIO_CTX = 768
WHISPER_BEAM_SIZE = 5
WHISPER_BEST_OF = 2   # was 5 — each pass ~1.3s on i7-7700, 5→2 saves ~4s latency

# Whisper initial prompt — biases toward home assistant vocabulary.
# Not a constraint, just a probability hint for ambiguous phonemes.
# Edit ~/assistant/whisper_prompt.txt to update (one line, plain text).
# The file is the source of truth — also used by the whisper-server wrapper.
import os as _os
_whisper_prompt_path = _os.path.join(_os.path.dirname(__file__), 'whisper_prompt.txt')
try:
    with open(_whisper_prompt_path, 'r') as _f:
        WHISPER_INITIAL_PROMPT = _f.read().strip()
except OSError:
    WHISPER_INITIAL_PROMPT = (
        "Dobby, turn on, turn off, switch on, switch off, kitchen lights, "
        "lounge lights, living room, bedroom, office, hallway, woodshed, "
        "shed, garage, garden, heatpump, "
        "dim, brightness, thermostat, temperature, alarm, timer, "
        "weather, forecast, set, cancel, volume, play, stop, pause"
    )

# ── MVDR beamforming ────────────────────────────────────────────────
# lama-side MVDR replaces ESP32-S3 beamformed output (channel 4).
# Uses all 4 raw mic channels for higher-fidelity spatial filtering.
MVDR_ENABLED             = False  # disabled — ch3 alone 20dB better than MVDR, suspect 3 fake mics
MVDR_FALLBACK_CHANNEL    = 1      # ch1 is clearest/cleanest mic (ch0 also good; ch2-3 noisy)
MVDR_SOURCE_AZIMUTH_DEG  = 0.0    # broadside — user directly in front
MVDR_SOURCE_ELEVATION_DEG = 18.0  # average sitting/standing at ~2m distance
MVDR_NFFT                = 256    # 16 ms at 16 kHz
MVDR_MIC_POSITIONS       = [
    [ 0.000, 0.000,  0.030],   # ch0 — mic 1 top
    [ 0.030, 0.000,  0.000],   # ch1 — mic 2 right
    [ 0.000, 0.000, -0.030],   # ch2 — mic 3 bottom
    [-0.030, 0.000,  0.000],   # ch3 — mic 4 left
]

# ── VAD thresholds rescaled for float32 [-1, 1] range ────────────────
# Original int16 values (for reference):
#   SILENCE_THRESHOLD=128 → 128/32768 ≈ 0.0039
#   SPEECH_ONSET_RMS=200  → 200/32768 ≈ 0.0061
#   MIN_EARLY_ONSET_RMS=2200 → 2200/32768 ≈ 0.0671
VAD_SILENCE_THRESHOLD_F32   = 128   / 32768.0
VAD_SPEECH_ONSET_RMS_F32    = 200   / 32768.0
VAD_MIN_EARLY_ONSET_RMS_F32 = 2200.0 / 32768.0
# Spurious-text rejection threshold (int16 430 → float32)
# ── SyncStreamer v2 (UDP audio, replaces Snapcast) ─────────────────
SYNCSTREAMER_AUDIO_PORT      = 5005       # audio stream port
SYNCSTREAMER_ANNOUNCE_PORT   = 5006       # control + registration port
SYNCSTREAMER_FRAMES_PER_PKT  = 256        # 5.333 ms @ 48 kHz
SYNCSTREAMER_SAMPLE_RATE     = 48000
SYNCSTREAMER_SERVER_LEAD_US  = 400_000    # 400 ms lead for client buffering
SYNCSTREAMER_PACKETS_PER_SEC = 187.5      # 48000/256

SPURIOUS_MAX_MEAN_RMS_F32   = 430.0 / 32768.0

# Latency metrics reporting
LATENCY_STATS_WINDOW = 50
LATENCY_STATS_REPORT_EVERY = 5

# Piper settings for dobby model
PIPER_MODEL_PATH = str(Path.home() / "tts/dobby_final.onnx")
PIPER_NOISE_SCALE = 0.3
PIPER_NOISE_W = 0.5
PIPER_LENGTH_SCALE = 1.0

# Kokoro ONNX TTS settings
ENABLE_TTS = True
TTS_ECHO_TRANSCRIPT = True
KOKORO_MODEL_PATH = str(PROJECT_DIR / 'models/kokoro/kokoro-v1.0.onnx')
KOKORO_VOICES_PATH = str(PROJECT_DIR / 'models/kokoro/voices-v1.0.bin')
# KOKORO_VOICE = 'bf_lily'
KOKORO_VOICE = 'bm_fable'
KOKORO_LANG = 'en-gb'
# KOKORO_SPEED = 1.0
KOKORO_SPEED = 0.70
# Pitch shift in semitones (positive = higher, e.g. 3 = ~Dobby-like)
KOKORO_PITCH_SHIFT_SEMITONES = 3

# Writes synthesized WAV files here. Playback/streaming can consume these.
TTS_OUTPUT_DIR = '/tmp'
ENABLE_LOCAL_TTS_PLAYBACK = False
# Optional explicit playback command. Use {wav} placeholder, e.g.:
# TTS_PLAYBACK_CMD = 'aplay -q {wav}'
TTS_PLAYBACK_CMD = ''

# Host -> Dobby speaker stream (SPK1 on UDP 4003)
ENABLE_UDP_TTS_STREAM = False
UDP_TTS_AUDIO_PORT = 4003
UDP_TTS_SAMPLE_RATE = 16000
UDP_TTS_CHANNELS = 1
UDP_TTS_FRAMES_PER_PACKET = 128
UDP_TTS_REALTIME = True
UDP_TTS_REALTIME_FACTOR = 1.0
UDP_TTS_START_DELAY_S = 0.12
UDP_TTS_END_GRACE_S = 0.60
# UDP_TTS_END_GRACE_S = 0.35
UDP_TTS_GAIN = 0.42
# UDP_TTS_SOFT_CLIP = False
UDP_TTS_SOFT_CLIP = True
UDP_TTS_FADE_MS = 20.0
UDP_TTS_PRE_SILENCE_PACKETS = 4
UDP_TTS_POST_SILENCE_PACKETS = 16

# ── Snapcast audio output ──────────────────────────────────────────
ENABLE_SNAPCAST = True
SNAPCAST_TCP_HOST = '127.0.0.1'
SNAPCAST_TCP_PORT = 4953
SNAPCAST_SAMPLE_RATE = 48000
SNAPCAST_GAIN = 0.55
SNAPCAST_SOFT_CLIP = True
SNAPCAST_FADE_MS = 20.0
SNAPCAST_PRE_SILENCE_MS = 150

# ── Barge-in (duck speakers during wakeword) ───────────────────────
ENABLE_BARGE_IN = True
BARGE_IN_MUTE_PORT = 4000
BARGE_IN_ESP32_IPS = ['192.168.5.255']
BARGE_IN_RESTORE_DELAY_MS = 80

# Local command interpreter (Ollama JSON extraction + MQTT execution)
LOCAL_INTENT_MODEL = 'qwen2.5:1.5b'
# Response generation uses the cloud; the local intent model stays small.
OLLAMA_BASE_URL = CRED_OLLAMA_BASE_URL
OLLAMA_REQUEST_TIMEOUT_S = 8.0
OLLAMA_KEEP_ALIVE = '30m'
COMMAND_CONFIDENCE_THRESHOLD = 0.70
DEVICE_REGISTRY_PATH = str(PROJECT_DIR / 'device_registry.json')
SENSOR_REGISTRY_PATH = str(PROJECT_DIR / 'sensor_registry.json')
SENSOR_CONDITION_TIMEOUT_S = 24 * 60 * 60
CLOUD_FALLBACK_MESSAGE = 'I will use cloud reasoning for that request.'
CLOUD_MIN_WORDS_FORWARD = 3

# Assistant persona/context for cloud responses
ASSISTANT_NAME = 'Nabu'
ASSISTANT_LOCATION = 'Home assistant node at lama, with Spomena frontend speaker'

# DeepSeek OpenAI-compatible cloud LLM (API key comes from credentials.py)
CLOUD_LLM_BASE_URL = 'https://api.deepseek.com'
CLOUD_LLM_MODEL = 'deepseek-chat'
CLOUD_LLM_MAX_TOKENS = 300
CLOUD_LLM_TIMEOUT_S = 12.0
CLOUD_LLM_API_KEY = (CRED_DEEPSEEK_API_KEY or '').strip()

# Cloud provider routing
# Supported: 'deepseek' (default), 'openclaw'
CLOUD_PROVIDER = 'openclaw'
CLOUD_PROVIDER_FALLBACK = 'deepseek'

# OpenClaw service endpoint
OPENCLAW_BASE_URL = CRED_OPENCLAW_BASE_URL
OPENCLAW_CHAT_PATH = '/v1/chat/completions'
OPENCLAW_TIMEOUT_S = 90.0
OPENCLAW_AUTH_TOKEN = (CRED_OPENCLAW_AUTH_TOKEN or '').strip()

# MQTT broker for device/sensor control
MQTT_BROKER_HOST = CRED_MQTT_BROKER_HOST
MQTT_BROKER_PORT = CRED_MQTT_BROKER_PORT
MQTT_CLIENT_ID = 'assistant_command_router'
MQTT_USERNAME = CRED_MQTT_USERNAME
MQTT_PASSWORD = CRED_MQTT_PASSWORD

ENABLE_MQTT_NOTIFICATIONS = True
MQTT_NOTIFICATION_TOPIC = 'assistant/notify/speak'

SOUNDS_DIR = str(PROJECT_DIR / 'sounds')

# ── Web server ─────────────────────────────────────────────────────
ENABLE_WEB_SERVER = True
WEB_SERVER_HOST = '0.0.0.0'
WEB_SERVER_PORT = 8080
WEB_RECORDINGS_DIR = str(PROJECT_DIR / 'testwavs')
# Sync destination for 4ch diagnostic WAVs
TESTWAVS_SYNC_TARGET = '/mnt/molly/development/lama/testwavs'

# ── Jellyfin music ──────────────────────────────────────────────────
JELLYFIN_SERVER_URL      = 'http://192.168.5.155:8096'
JELLYFIN_API_KEY         = (CRED_JELLYFIN_API_KEY or '').strip()
JELLYFIN_SEARCH_LIMIT    = 5
JELLYFIN_STREAM_CONTAINER = 'wav'
JELLYFIN_REQUEST_TIMEOUT_S = 10

# Cloud-generated response cache: seconds between refreshes, randomized in this range.
RESPONSE_REFRESH_ENABLED = True
RESPONSE_REFRESH_MIN_SECONDS = 1800
RESPONSE_REFRESH_MAX_SECONDS = 3600
RESPONSE_REFRESH_TIMEOUT_SECONDS = 120
RESPONSE_REFRESH_MAX_TOKENS = 1600
RESPONSE_CACHE_DIR = STATE_DIR / 'responses'
RESPONSE_SEED_PATH = PROJECT_DIR / 'defaults/dobby_responses.json'

# Private Unix socket: CLI controls the pipeline's existing music controller.
MUSIC_IPC_PATH = STATE_DIR / 'music.sock'
MUSIC_IPC_TIMEOUT_SECONDS = 120

# before: acknowledge then publish; after: publish then report success/failure.
DEVICE_ACKNOWLEDGMENT_ORDER = 'before'
MQTT_PUBLISH_TIMEOUT_SECONDS = 5
AUTOMATION_DB_PATH = STATE_DIR / 'automation.sqlite3'
# Restart policy for timers already overdue: 'skip' or 'run'.
AUTOMATION_OVERDUE_POLICY = 'skip'
# Scheduling is acknowledged immediately; failures are always reported.
AUTOMATION_REPORT_SUCCESS = False
