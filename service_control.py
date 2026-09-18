"""Shared system-service operations for diagnostic tools."""
import os
from pathlib import Path
import subprocess
import sys
from config import STT_TEST_MODE_FILE

SERVICE = 'assistant.service'

def set_test_mode(enabled):
    flag = Path(STT_TEST_MODE_FILE)
    flag.parent.mkdir(parents=True, exist_ok=True)
    if enabled:
        flag.write_text('1\n')
    else:
        flag.unlink(missing_ok=True)
    prefix = [] if os.geteuid() == 0 else ['sudo']
    subprocess.run(prefix + ['systemctl', 'restart', SERVICE], check=True)

if __name__ == '__main__':
    if len(sys.argv) != 2 or sys.argv[1] not in ('on', 'off'):
        raise SystemExit('Usage: service_control.py on|off')
    set_test_mode(sys.argv[1] == 'on')
