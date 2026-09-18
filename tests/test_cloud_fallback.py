import unittest
from unittest.mock import patch
from cloud_provider import OpenClawCloudProvider, speak_fallback

class Stream:
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def __iter__(self):
        yield b'data: {"choices":[{"delta":{"content":"First sentence. "}}]}\n'
        raise OSError('stream interrupted')

class FallbackTests(unittest.TestCase):
    def test_partial_stream_fallback_does_not_repeat(self):
        p = OpenClawCloudProvider()
        said = []
        with patch('cloud_provider.urllib.request.urlopen', return_value=Stream()), patch.object(p, 'reply', return_value='First sentence. Second sentence.'):
            result = p.stream_reply('question', [], said.append)
        self.assertEqual(said, ['First sentence.', 'Second sentence.'])
        self.assertEqual(result, 'First sentence. Second sentence.')

    def test_failure_before_first_sentence_speaks_fallback(self):
        p = OpenClawCloudProvider()
        said = []
        with patch('cloud_provider.urllib.request.urlopen', side_effect=OSError), patch.object(p, 'reply', return_value='Dobby cannot reach the cloud.'):
            p.stream_reply('question', [], said.append)
        self.assertEqual(said, ['Dobby cannot reach the cloud.'])
