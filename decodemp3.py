#!/usr/bin/env python3
import json
import base64
import sys
import subprocess
import tempfile
from pathlib import Path

if len(sys.argv) != 2:
    print("Usage: decodemp3.py <output_stem>")
    print("       cat response.json | decodemp3.py <output_stem>")
    print("Example: decodemp3.py chime")
    print("         pbpaste | decodemp3.py doorbell")
    sys.exit(1)

output_path = Path(sys.argv[1]).with_suffix('.wav')

if not sys.stdin.isatty():
    data = json.load(sys.stdin)
else:
    data = json.load(open('response.json'))

decoded = [base64.b64decode(a) for a in data['audioFiles']]
best_idx, audio_bytes = max(enumerate(decoded), key=lambda x: len(x[1]))
print(f"Selected blob [{best_idx}] of {len(decoded)} ({len(audio_bytes)} bytes)")

with tempfile.NamedTemporaryFile(suffix='.mp3', delete=False) as tmp:
    tmp.write(audio_bytes)
    tmp_path = Path(tmp.name)

try:
    result = subprocess.run([
        'ffmpeg', '-y',
        '-i', str(tmp_path),
        '-ar', '16000',
        '-ac', '1',
        '-sample_fmt', 's16',
        str(output_path)
    ], capture_output=True, text=True)
finally:
    tmp_path.unlink()

if result.returncode == 0:
    print(f"Written: {output_path}")
else:
    print(f"ffmpeg error: {result.stderr}")
    sys.exit(1)
