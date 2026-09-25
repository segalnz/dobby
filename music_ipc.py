"""Private, same-user JSON IPC for the running music controller."""
import json
import logging
import os
from pathlib import Path
import socket
import socketserver
import stat
import threading

MAX_MESSAGE = 65536
log = logging.getLogger(__name__)


def dispatch_music(music, request):
    if not isinstance(request, dict):
        raise ValueError('Request must be an object')
    action = request.get('command')
    with music._lock:
        if action == 'status':
            track = music._current_track or {}
            response = track.get('Name', 'Nothing is playing.')
        elif action == 'search':
            query = request.get('query')
            if not isinstance(query, str) or not 1 <= len(query.strip()) <= 500:
                raise ValueError('A nonempty query up to 500 characters is required')
            items = music._jellyfin.search(query)
            return {'ok': True, 'items': [{'name': i.get('Name'), 'type': i.get('Type')}
                                          for i in items[:10]]}
        else:
            if action == 'play':
                query = request.get('query')
                kind = request.get('type')
                if not isinstance(query, str) or not 1 <= len(query.strip()) <= 500:
                    raise ValueError('A nonempty query up to 500 characters is required')
                if kind not in (None, 'artist', 'album', 'track', 'playlist'):
                    raise ValueError('Invalid music type')
                text = 'play ' + ((kind + ' ') if kind else '') + query
            elif action == 'control':
                control = request.get('action')
                if control not in ('pause', 'stop', 'resume', 'next', 'previous', 'now'):
                    raise ValueError('Invalid playback action')
                text = 'what is playing' if control == 'now' else control
            else:
                raise ValueError('Unknown command')
            music.is_music_command(text)
            result = music.handle_command(text)
            if result is None:
                return {'ok': False, 'error': 'Music command could not be handled'}
            outcome, response = result
            if outcome == 'error':
                return {'ok': False, 'error': response}
        return {'ok': True, 'response': response, 'state': music.state,
                'queue_length': len(music.queue)}


class MusicServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    def __init__(self, path, music):
        self.music = music
        super().__init__(str(path), MusicHandler)


class MusicHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(10)
        try:
            raw = self.rfile.readline(MAX_MESSAGE + 1)
            if len(raw) > MAX_MESSAGE or not raw.endswith(b'\n'):
                raise ValueError('Invalid request framing/size')
            result = dispatch_music(self.server.music, json.loads(raw))
        except (ValueError, TypeError) as exc:
            result = {'ok': False, 'error': str(exc)}
        except Exception:
            log.exception('Music IPC failed')
            result = {'ok': False, 'error': 'Music operation failed; check assistant logs'}
        try:
            self.wfile.write(json.dumps(result).encode() + b'\n')
        except OSError:
            pass


def start_music_ipc(music, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not stat.S_ISSOCK(path.lstat().st_mode):
            raise RuntimeError('Music IPC path exists and is not a socket')
        with socket.socket(socket.AF_UNIX) as probe:
            try:
                probe.connect(str(path))
            except ConnectionRefusedError:
                path.unlink()
            else:
                raise RuntimeError('Another music IPC server is already running')
    server = MusicServer(path, music)
    os.chmod(path, 0o600)
    threading.Thread(target=server.serve_forever, daemon=True, name='music-ipc').start()
    return server


def request_music(path, request, timeout=120):
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(timeout)
        client.connect(str(path))
        client.sendall(json.dumps(request).encode() + b'\n')
        with client.makefile('rb') as response:
            raw = response.readline(MAX_MESSAGE + 1)
    if len(raw) > MAX_MESSAGE or not raw.endswith(b'\n'):
        raise ValueError('Invalid music server response')
    return json.loads(raw)
