import json
from pathlib import Path
import tempfile
import unittest
from response_cache import ResponseCache, validate_templates

class CacheTests(unittest.TestCase):
    def test_rotation_rejection_and_recovery(self):
        with tempfile.TemporaryDirectory() as d:
            seed = Path(d) / 'seed.json'
            seed.write_text(json.dumps({'on': ['Old {device}'], 'generic': ['Dobby ready']}))
            cache = ResponseCache(Path(d) / 'responses', seed)
            cache.update({'on': ['New {device}'], 'generic': ['Dobby listens']})
            before = cache.current.read_bytes()
            with self.assertRaises(ValueError):
                cache.update({'on': ['Broken {unknown}']})
            self.assertEqual(cache.current.read_bytes(), before)
            cache.update({'on': ['Next {device}'], 'generic': ['Dobby waits']})
            self.assertEqual(json.loads(cache.previous.read_text())['on'][0], 'New {device}')
            self.assertEqual(sorted(p.name for p in cache.directory.iterdir()), ['current.json', 'previous.json'])
            cache.current.write_text('broken')
            self.assertEqual(cache.get()['on'][0], 'New {device}')

    def test_placeholder_injection_is_rejected(self):
        for text in ['Bad {device.__class__}', 'Bad {value!r}', 'Bad {value:9999999}', 'Bad {']:
            with self.assertRaises(ValueError):
                validate_templates({'on': [text]})


class RefreshTests(unittest.TestCase):
    def test_configured_endpoint_and_failed_refresh_preserves_cache(self):
        import io
        import config
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d:
            seed = Path(d) / 'seed.json'
            seed.write_text(json.dumps({'on': ['Old {device}']}))
            cache = ResponseCache(Path(d) / 'responses', seed)
            reply = {'choices': [{'message': {'content': '{"on":["New {device}"]}'}}]}
            with patch.object(config, 'CLOUD_LLM_API_KEY', 'test-only'), patch.object(config, 'CLOUD_LLM_BASE_URL', 'http://example.invalid:1234'), patch('response_cache.urllib.request.urlopen', return_value=io.BytesIO(json.dumps(reply).encode())) as request:
                cache.refresh({'lights'})
            self.assertEqual(request.call_args.args[0].full_url, 'http://example.invalid:1234/chat/completions')
            payload = json.loads(request.call_args.args[0].data)
            self.assertEqual(payload['response_format'], {'type': 'json_object'})
            self.assertNotIn('tools', payload)
            before = cache.current.read_bytes()
            with patch.object(config, 'CLOUD_LLM_API_KEY', 'test-only'), patch('response_cache.urllib.request.urlopen', side_effect=OSError('offline')):
                with self.assertRaises(OSError): cache.refresh({'lights'})
            self.assertEqual(cache.current.read_bytes(), before)
