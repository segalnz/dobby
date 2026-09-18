import importlib.util
from pathlib import Path
import runpy
import sys
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

class ConfigurationTests(unittest.TestCase):
    def test_partial_credentials_preserve_supplied_settings(self):
        creds = types.ModuleType('credentials')
        creds.MQTT_BROKER_HOST = 'example-broker'
        with patch.dict(sys.modules, {'credentials': creds}):
            settings = runpy.run_path(str(ROOT / 'config.py'))
        self.assertEqual(settings['MQTT_BROKER_HOST'], 'example-broker')
        self.assertEqual(settings['JELLYFIN_API_KEY'], '')

    def test_example_defines_every_credential(self):
        import ast
        example = runpy.run_path(str(ROOT / 'credentials.example.py'))
        tree = ast.parse((ROOT / 'config.py').read_text())
        names = [n.args[0].value for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id == '_credential']
        self.assertTrue(set(names) <= set(example))

    def test_launcher_uses_the_configured_model(self):
        import config
        import run_whisper_server
        with patch.object(config, 'WHISPER_MODEL', '/tmp/custom-model.bin'):
            args = run_whisper_server.command()
        self.assertEqual(args[args.index('-m') + 1], '/tmp/custom-model.bin')

class UnitRenderingTests(unittest.TestCase):
    def test_venv_interpreter_symlink_is_not_resolved(self):
        import tempfile
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            venv_python = Path(d) / 'venv/bin/python'
            venv_python.parent.mkdir(parents=True)
            venv_python.symlink_to(sys.executable)
            output = Path(d) / 'units'
            subprocess.run([sys.executable, str(ROOT / 'deploy/render_units.py'),
                            '--python', str(venv_python), '--output', str(output)], check=True)
            unit = (output / 'assistant.service').read_text()
            self.assertIn('ExecStart=' + str(venv_python) + ' ', unit)

if __name__ == '__main__':
    unittest.main()
