#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
SERVICE=assistant.service
PYTHON="${ASSISTANT_PYTHON:-$HOME/venvs/assistant/bin/python}"
if [[ ! -x "$PYTHON" ]]; then PYTHON="$DIR/.venv/bin/python"; fi
as_root() { if [[ $EUID -eq 0 ]]; then "$@"; else sudo "$@"; fi; }
case "${1:-}" in
  start|stop|restart)
    as_root systemctl "$1" "$SERVICE"
    systemctl --no-pager --full status "$SERVICE" || [[ "$1" == stop ]]
    ;;
  status) systemctl --no-pager --full status "$SERVICE" ;;
  logs) journalctl -u "$SERVICE" -f ;;
  stt-test)
    shift
    cleanup() { "$PYTHON" "$DIR/service_control.py" off; }
    trap cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    "$PYTHON" "$DIR/service_control.py" on
    "$PYTHON" "$DIR/stt_regression_test.py" "$@"
    ;;
  testphrases) shift; exec "$PYTHON" "$DIR/testphrases.py" "$@" ;;
  *) echo "Usage: $0 {start|stop|restart|status|logs|stt-test|testphrases}"; exit 1 ;;
esac
