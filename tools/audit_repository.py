"""Report sensitive filenames and likely literal secrets without printing their values."""
import ast
from pathlib import Path
import re
import subprocess

root = Path(__file__).resolve().parents[1]
def git(*args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True)
paths = git('ls-files').splitlines()
findings = []
for name in paths:
    if any(x in name.lower() for x in ('credentials.cpython',)) or name == 'credentials.py':
        findings.append((name, 'private credential file'))
    p = root / name
    if p.suffix != '.py' or not p.is_file():
        continue
    try: tree = ast.parse(p.read_text())
    except (SyntaxError, UnicodeError): continue
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str): continue
        names = [n.id for t in node.targets for n in ast.walk(t) if isinstance(n, ast.Name)]
        if any(re.search(r'(api_key|password|auth_token|secret)$', n, re.I) for n in names) and len(node.value.value) > 6:
            if 'example' not in name and not node.value.value.startswith(('YOUR_', '<')):
                findings.append((f'{name}:{node.lineno}', 'possible literal secret; review privately'))
print('CURRENT TRACKED TREE')
for name, reason in findings: print(name + ': ' + reason)
print('Potential findings:', len(findings))
print('HISTORY (filenames only)')
for name in ('credentials.py', '__pycache__/credentials.cpython-312.pyc'):
    revisions = git('log', '--all', '--format=%h', '--', name).splitlines()
    print(name + ': ' + str(len(revisions)) + ' commits touching this path')
print('This is a targeted audit, not proof that all historical secrets were found.')
