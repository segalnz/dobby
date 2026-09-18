# Copy this file to credentials.py and fill real secrets.
# Keep credentials.py local and out of source control.

# Network / endpoint overrides
ESP32_IP = "192.168.5.66"
HOST_IP = "192.168.5.75"
WHISPER_SERVER_HOST = "127.0.0.1"
WHISPER_SERVER_PORT = 8081
OLLAMA_BASE_URL = "http://127.0.0.1:11434"

# MQTT credentials
MQTT_USERNAME = ""
MQTT_PASSWORD = ""
MQTT_BROKER_HOST = "192.168.5.160"
MQTT_BROKER_PORT = 1883

# Optional cloud key.
# Store this in credentials.py (single source for credentials).
DEEPSEEK_API_KEY = ""

# Optional OpenClaw local service settings
OPENCLAW_BASE_URL = "http://127.0.0.1:18789"
OPENCLAW_AUTH_TOKEN = ""

# Jellyfin media access (optional when music is not used).
JELLYFIN_API_KEY = ""
