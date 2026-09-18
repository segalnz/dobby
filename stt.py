# ~/assistant/stt.py
import subprocess
import tempfile
import wave
import os
import numpy as np
import logging
import urllib.request
import urllib.error
from config import *

log = logging.getLogger(__name__)


def _write_wav(audio: np.ndarray) -> str:
    """Write audio array to a temp WAV file and return the path."""
    with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
        wav_path = f.name
    with wave.open(wav_path, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio.tobytes())
    return wav_path


def _transcribe_server(wav_path: str) -> str:
    """POST WAV file to whisper-server /inference endpoint (multipart form)."""
    import io
    BOUNDARY = b'----WsSTTBoundary'
    with open(wav_path, 'rb') as f:
        wav_bytes = f.read()

    body = (
        b'--' + BOUNDARY + b'\r\n'
        b'Content-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
        b'Content-Type: audio/wav\r\n\r\n'
        + wav_bytes + b'\r\n'
        b'--' + BOUNDARY + b'\r\n'
        b'Content-Disposition: form-data; name="response_format"\r\n\r\n'
        b'json\r\n'
        b'--' + BOUNDARY + b'--\r\n'
    )

    url = f'http://{WHISPER_SERVER_HOST}:{WHISPER_SERVER_PORT}/inference'
    req = urllib.request.Request(
        url,
        data=body,
        headers={'Content-Type': f'multipart/form-data; boundary={BOUNDARY.decode()}'},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        import json
        data = json.loads(resp.read().decode())
        return data.get('text', '').strip()


def _transcribe_cli(wav_path: str) -> str:
    """Invoke whisper-cli as a subprocess (fallback)."""
    args = [
        WHISPER_BIN,
        '-m', WHISPER_MODEL,
        '-f', wav_path,
        '--no-timestamps',
        '--language', 'en',
        '-t', str(WHISPER_THREADS),
        '-bo', str(WHISPER_BEST_OF),
        '-bs', str(WHISPER_BEAM_SIZE),
        '-ac', str(WHISPER_AUDIO_CTX),
        '-nt',
    ]
    try:
        prompt = WHISPER_INITIAL_PROMPT
    except NameError:
        prompt = None
    if prompt:
        args.extend(['--prompt', prompt])
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        log.error('whisper-cli error: %s', result.stderr[:200])
        return ''
    return result.stdout.strip()


def transcribe(audio: np.ndarray) -> str:
    """Transcribe audio using whisper-server if available, else whisper-cli."""
    wav_path = _write_wav(audio)
    try:
        if USE_WHISPER_SERVER:
            try:
                text = _transcribe_server(wav_path)
                log.info('STT (server): %s', text)
                return text
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                log.warning('whisper-server unavailable (%s), falling back to CLI', e)
        text = _transcribe_cli(wav_path)
        log.info('STT (cli): %s', text)
        return text
    finally:
        try:
            os.unlink(wav_path)
        except OSError:
            pass
