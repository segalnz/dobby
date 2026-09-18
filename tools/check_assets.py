"""Verify locally provisioned model assets against the deployment manifest."""
import hashlib
import json
from pathlib import Path
import sys
root = Path(__file__).resolve().parents[1]
manifest = json.loads((root / 'deploy/assets.json').read_text())
failed = False
for asset in manifest['assets']:
    path = root / asset['path']
    if not path.is_file():
        print('MISSING:', asset['path']); failed = True; continue
    with path.open('rb') as f:
        digest = hashlib.file_digest(f, 'sha256').hexdigest()
    ok = digest == asset['sha256'] and path.stat().st_size == asset['bytes']
    print('OK:' if ok else 'MISMATCH:', asset['path'])
    failed |= not ok
raise SystemExit(int(failed))
