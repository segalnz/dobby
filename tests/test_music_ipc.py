import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from music_ipc import start_music_ipc, request_music

class FakeMusic:
    def __init__(self):
        self._lock = threading.RLock()
        self._current_track = {'Name': 'Example track'}
        self.state = 'playing'
        self.queue = [1, 2]
        self.commands = []
    def is_music_command(self, text):
        return True
    def handle_command(self, text):
        self.commands.append(text)
        self.state = 'paused'
        return self.state, 'Paused.'

class IPCTests(unittest.TestCase):
    def test_shared_controller_and_validation(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'music.sock'
            music = FakeMusic()
            server = start_music_ipc(music, path)
            try:
                self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
                status = request_music(path, {'command': 'status'})
                self.assertEqual(status['queue_length'], 2)
                self.assertTrue(request_music(path, {'command': 'control', 'action': 'pause'})['ok'])
                self.assertEqual(music.commands, ['pause'])
                self.assertEqual(request_music(path, {'command': 'status'})['state'], 'paused')
                self.assertFalse(request_music(path, {'command': 'control', 'action': 'shell'})['ok'])
                self.assertEqual(music.commands, ['pause'])
            finally:
                server.shutdown()
                server.server_close()
