#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
exec "${ASSISTANT_PYTHON:-python3}" "$DIR/run_whisper_server.py" "$@"
