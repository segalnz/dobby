"""Render system units without installing or restarting services."""
import argparse
import getpass
from pathlib import Path
import sys

p = argparse.ArgumentParser()
p.add_argument('--project', type=Path, default=Path(__file__).resolve().parents[1])
p.add_argument('--python', type=Path, default=Path(sys.executable))
p.add_argument('--user', default=getpass.getuser())
p.add_argument('--output', type=Path, required=True)
a = p.parse_args()
values = {'@PROJECT@': str(a.project.resolve()), '@PYTHON@': str(a.python.absolute()), '@USER@': a.user}
# Restrict template tokens to plain unit-safe paths/usernames.
if any(any(c.isspace() or c in '%"\\' for c in v) for v in values.values()):
    p.error('Use paths/usernames without whitespace, quotes, backslashes or percent signs')
a.output.mkdir(parents=True, exist_ok=True)
for source in Path(__file__).parent.glob('*.service.in'):
    text = source.read_text()
    for key, value in values.items():
        text = text.replace(key, value)
    (a.output / source.name.removesuffix('.in')).write_text(text)
