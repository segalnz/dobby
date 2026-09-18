#!/usr/bin/env bash
# Provision Python dependencies only; does not install units or start services.
set -euo pipefail
DIR="$(cd -- "$(dirname -- "$0")/.." && pwd)"
PYTHON="${ASSISTANT_BOOTSTRAP_PYTHON:-python3.12}"
"$PYTHON" -m venv "$DIR/.venv"
"$DIR/.venv/bin/python" -m pip install pip==26.1.1
"$DIR/.venv/bin/python" -m pip install -r "$DIR/requirements.lock"
"$DIR/.venv/bin/python" -m pip check
