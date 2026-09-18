"""Export reviewed source only, without Git history or private runtime assets."""
import argparse
from pathlib import Path
import shutil
import subprocess

root = Path(__file__).resolve().parents[1]
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('destination', type=Path)
a = p.parse_args()
dest = a.destination.resolve()
if dest == root or root in dest.parents:
    p.error('Export outside the live project')
if dest.exists():
    p.error('Destination must not already exist')
paths = subprocess.check_output(['git', '-C', str(root), 'ls-files'], text=True).splitlines()
allowed_dirs = {'deploy', 'defaults', 'docs', 'tests', 'tools', 'syncstreamer', 'unused'}
allowed_root = {'.gitignore', 'README.md', 'requirements.txt', 'requirements.lock',
                'credentials.example.py', 'device_registry.example.json', 'sensor_registry.example.json',
                'frontend_README.md', 'server_specification_v2.txt', 'dobby_voice_instructions.txt',
                'whisper_prompt.txt', 'jellyfin_translations.txt', 'templates_notes.md'}
selected=[]
for name in paths:
    rel = Path(name)
    src = root / rel
    if not src.is_file() or src.is_symlink() or '__pycache__' in rel.parts: continue
    if rel.parts[0] in allowed_dirs and src.suffix in {'.py', '.md', '.txt', '.json', '.sh', '.in'}:
        selected.append(rel)
    elif len(rel.parts) == 1 and (name in allowed_root or (src.suffix in {'.py', '.sh'} and name != 'credentials.py')):
        selected.append(rel)
dest.mkdir(parents=True)
for rel in selected:
    target=dest/rel
    target.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(root/rel,target)
print(f'Exported {len(selected)} source files to {dest}; no Git history included.')
print('Review docs/repository-preparation.md and choose a license before publication.')
