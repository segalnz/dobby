import io
import json
import unittest
import urllib.error
from unittest.mock import Mock, patch
from jellyfin_music import JellyfinClient, JellyfinError, MusicController
from music_ipc import dispatch_music


class JellyfinErrorTests(unittest.TestCase):
    def test_search_uses_full_authorization_header(self):
        def server(request, **kwargs):
            auth = request.get_header('Authorization', '')
            if 'Token="test-only"' not in auth or not auth.startswith('MediaBrowser '):
                raise urllib.error.HTTPError(request.full_url, 401, 'Unauthorized', {}, None)
            self.assertIn('Client="Dobby"', auth)
            self.assertIsNone(request.get_header('X-emby-token'))
            return io.BytesIO(json.dumps({'Items': [
                {'Name': 'Pink Floyd', 'Type': 'MusicArtist'}]}).encode())
        with patch('urllib.request.urlopen', side_effect=server):
            items = JellyfinClient(api_key='test-only').search('Pink Floyd')
        self.assertEqual(items[0]['Name'], 'Pink Floyd')

    def test_auth_rejection_is_not_empty_search(self):
        for code in (401, 403):
            with self.subTest(code=code), patch('urllib.request.urlopen', side_effect=
                    urllib.error.HTTPError('http://example/Items', code, 'Rejected', {}, None)):
                with self.assertRaisesRegex(JellyfinError, 'API key'):
                    JellyfinClient(api_key='test-only').search('Pink Floyd')

    def test_successful_empty_search(self):
        with patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps({'Items': []}).encode())):
            self.assertEqual(JellyfinClient().search('Unknown artist'), [])

    def test_voice_auth_failure_is_spoken_once(self):
        music = MusicController()
        music._speak = Mock()
        music._jellyfin = Mock()
        music._jellyfin.ping.return_value = True
        music._jellyfin.search.side_effect = JellyfinError('Jellyfin rejected my API key.')
        result = music.handle_command('ask Jellyfin to play Pink Floyd.')
        self.assertEqual(result, ('error', 'Jellyfin rejected my API key.'))
        music._speak.assert_called_once_with(result[1])
        self.assertEqual(music.queue, [])
        music._speak.reset_mock()
        result = dispatch_music(music, {'command': 'play', 'query': 'Pink Floyd'})
        self.assertFalse(result['ok'])
        self.assertIn('API key', result['error'])

    def test_artist_auth_failure_does_not_cache_empty_library(self):
        music = MusicController()
        music._jellyfin = Mock()
        music._jellyfin._get.side_effect = JellyfinError('Rejected')
        with self.assertRaises(JellyfinError):
            music._get_artist_names()
        self.assertIsNone(music._artist_names_cache)
