#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
exec "$DIR/assistantctl.sh" testphrases "$@"
