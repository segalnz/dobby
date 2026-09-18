import logging
import os
import subprocess
import sys
import threading
import time

from flask import Flask, jsonify, request, render_template_string

from config import (
    # weatherdash

    HOST_IP,
    WEB_SERVER_HOST,
    WEB_SERVER_PORT,
    WEB_RECORDINGS_DIR,
    TESTWAVS_SYNC_TARGET,
)

log = logging.getLogger(__name__)

app = Flask(__name__)

_shared = {
    'recording_active': False,
    'recording_path': '',
    'recording_start_time': None,
    'recording_writer': None,
    'last_utterance': '',
    'last_response': '',
    'pipeline_state': 'IDLE',
    'audio_level': 0,
    'snapcast_test_running': False,
    'snapcast': None,
    'barge_in': None,
    'receiver': None,
    'tts': None,
}

STATUS_PAGE = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nabu Assistant</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,-apple-system,sans-serif;background:#1a1a2e;color:#e0e0e0;min-height:100vh}
.header{background:#16213e;padding:16px 24px;border-bottom:2px solid #0f3460}
.header h1{font-size:1.4em;color:#e94560}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px;padding:16px;max-width:1200px;margin:0 auto}
.card{background:#16213e;border-radius:8px;padding:16px;border:1px solid #0f3460}
.card h2{font-size:1.1em;color:#e94560;margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid #0f3460}
.row{display:flex;justify-content:space-between;align-items:center;padding:6px 0}
.label{color:#888;font-size:0.9em}
.value{color:#0f0;font-weight:600}
.value.warn{color:#ff0}
.value.bad{color:#f44}
.btn{padding:8px 16px;border:none;border-radius:4px;cursor:pointer;font-weight:600;font-size:0.9em;margin:4px}
.btn-primary{background:#e94560;color:#fff}
.btn-secondary{background:#0f3460;color:#e0e0e0}
.btn-danger{background:#c0392b;color:#fff}
.btn-success{background:#27ae60;color:#fff}
.btn:disabled{opacity:0.5;cursor:not-allowed}
.btn:hover:not(:disabled){filter:brightness(1.2)}
.audio-bar{width:100%;height:8px;background:#0f3460;border-radius:4px;margin-top:4px}
.audio-fill{height:100%;background:#e94560;border-radius:4px;transition:width 0.1s}
</style>
</head>
<body>
<div class="header"><h1>Nabu Assistant</h1></div>
<div class="grid">
<div class="card">
<h2>Pipeline Status</h2>
<div class="row"><span class="label">State</span><span class="value" id="state">{{ state }}</span></div>
<div class="row"><span class="label">Last Utterance</span><span class="value" id="utterance">{{ utterance }}</span></div>
<div class="row"><span class="label">Last Response</span><span class="value" id="response">{{ response }}</span></div>
<div class="row"><span class="label">Audio Level</span><span id="level">0</span></div>
<div class="audio-bar"><div class="audio-fill" id="audio-fill" style="width:0%"></div></div>
</div>
<div class="card">
<h2>Audio Recording</h2>
<div id="recording-status" class="row"><span class="label">Status</span><span class="value">{{ rec_status }}</span></div>
<div id="recording-path" class="row"><span class="label">File</span><span>{{ rec_file }}</span></div>
<button class="btn btn-primary" id="btn-record-start" onclick="recordStart()">Start 4ch Recording</button>
<button class="btn btn-danger" id="btn-record-stop" onclick="recordStop()">Stop</button>
<button class="btn btn-secondary" style="margin-top:8px" onclick="syncTestwavs()">Sync WAVs → Desktop</button>
<span id="sync-status"></span>
</div>
<div class="card">
<h2>Test Controls</h2>
<button class="btn btn-secondary" id="btn-snapcast-test" onclick="snapcastTest()">Run Snapcast Test</button>
<span id="snapcast-test-status"></span>
</div>
<div class="card">
<h2>TTS Test</h2>
<input type="text" id="tts-text" placeholder="Text to speak..." style="width:70%;padding:6px;border-radius:4px;border:1px solid #0f3460;background:#1a1a2e;color:#e0e0e0">
<button class="btn btn-secondary" onclick="ttsTest()">Speak</button>
</div>
<div class="card">
<h2>Sound Test</h2>
<input type="text" id="sound-name" placeholder="Sound name (e.g. doorbell)..." style="width:70%;padding:6px;border-radius:4px;border:1px solid #0f3460;background:#1a1a2e;color:#e0e0e0">
<button class="btn btn-secondary" onclick="soundTest()">Play Sound</button>
</div>
</div>
<script>
async function poll() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    document.getElementById('state').textContent = d.state || 'IDLE';
    document.getElementById('utterance').textContent = d.last_utterance || '—';
    document.getElementById('response').textContent = d.last_response || '—';
    document.getElementById('level').textContent = (d.audio_level || 0).toFixed(0);
    document.getElementById('audio-fill').style.width = Math.min(100, (d.audio_level || 0) / 100 * 100) + '%';
    document.getElementById('recording-status').innerHTML = '<span class="label">Status</span><span class="value">' + (d.recording_active ? 'Recording' : 'Inactive') + '</span>';
    document.getElementById('recording-path').innerHTML = '<span class="label">File</span><span>' + (d.recording_path || '—') + '</span>';
    document.getElementById('btn-record-start').disabled = d.recording_active;
    document.getElementById('btn-record-stop').disabled = !d.recording_active;
    document.getElementById('btn-snapcast-test').disabled = d.snapcast_test_running;
    document.getElementById('snapcast-test-status').textContent = d.snapcast_test_running ? ' Running...' : '';
  } catch(e) {}
}
setInterval(poll, 2000);
async function recordStart() {
  try { await fetch('/api/record/start', {method:'POST'}); } catch(e) {}
}
async function recordStop() {
  try { await fetch('/api/record/stop', {method:'POST'}); } catch(e) {}
}
async function snapcastTest() {
  try {
    document.getElementById('snapcast-test-status').textContent = ' Starting...';
    await fetch('/api/snapcast/test', {method:'POST'});
  } catch(e) {}
}
async function syncTestwavs() {
  try {
    document.getElementById('sync-status').textContent = ' Syncing...';
    await fetch('/api/testwavs/sync', {method:'POST'});
    setTimeout(() => document.getElementById('sync-status').textContent = ' ✓ Done', 2000);
  } catch(e) {}
}
async function ttsTest() {
  const text = document.getElementById('tts-text').value;
  if (!text) return;
  try { await fetch('/api/tts/test', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({text})}); } catch(e) {}
}
async function soundTest() {
  const sound = document.getElementById('sound-name').value;
  if (!sound) return;
  try { await fetch('/api/sound/test', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({sound})}); } catch(e) {}
}
</script>
</body>
</html>'''


@app.route('/')
def index():
    receiver = _shared.get('receiver')
    recording = receiver.is_recording() if receiver is not None else False
    return render_template_string(
        STATUS_PAGE,
        state=_shared.get('pipeline_state', 'IDLE'),
        utterance=_shared.get('last_utterance', '—'),
        response=_shared.get('last_response', '—'),
        rec_status='Recording' if recording else 'Inactive',
        rec_file=_shared.get('recording_path', '—'),
    )


@app.route('/api/status')
def api_status():
    receiver = _shared.get('receiver')
    recording = receiver.is_recording() if receiver is not None else False
    return jsonify({
        'state': _shared.get('pipeline_state', 'IDLE'),
        'last_utterance': _shared.get('last_utterance', ''),
        'last_response': _shared.get('last_response', ''),
        'audio_level': _shared.get('audio_level', 0),
        'recording_active': recording,
        'recording_path': _shared.get('recording_path', ''),
        'snapcast_test_running': _shared.get('snapcast_test_running', False),
    })


@app.route('/api/record/start', methods=['POST'])
def record_start():
    receiver = _shared.get('receiver')
    if receiver is None:
        return jsonify({'error': 'receiver not available'}), 503
    if receiver.is_recording():
        return jsonify({'status': 'already recording'}), 409
    ts = int(time.time())
    base = f'rec_{ts}'
    os.makedirs(WEB_RECORDINGS_DIR, exist_ok=True)
    ok = receiver.start_recording(WEB_RECORDINGS_DIR, base)
    if not ok:
        return jsonify({'error': 'failed to start'}), 500
    _shared['recording_active'] = True
    _shared['recording_path'] = base
    _shared['recording_start_time'] = time.time()
    log.info('4ch recording started: %s_*.wav', base)
    return jsonify({'status': 'started', 'base': base})


@app.route('/api/record/stop', methods=['POST'])
def record_stop():
    receiver = _shared.get('receiver')
    if receiver is None:
        return jsonify({'error': 'receiver not available'}), 503
    if not receiver.is_recording():
        return jsonify({'status': 'not recording'}), 409
    paths = receiver.stop_recording()
    _shared['recording_active'] = False
    _shared['recording_path'] = ''
    log.info('4ch recording stopped — %d files', len(paths) if paths else 0)
    return jsonify({'status': 'stopped', 'files': len(paths) if paths else 0})


@app.route('/api/record/status')
def record_status():
    receiver = _shared.get('receiver')
    active = receiver.is_recording() if receiver is not None else False
    return jsonify({
        'active': active,
        'path': _shared.get('recording_path', ''),
        'elapsed': (time.time() - _shared.get('recording_start_time', 0))
                    if active else 0,
    })


@app.route('/api/tts/test', methods=['POST'])
def tts_test():
    data = request.get_json() or {}
    text = data.get('text', '').strip()
    if not text:
        return jsonify({'error': 'text required'}), 400

    tts = _shared.get('tts')
    snapcast = _shared.get('snapcast')
    if tts is None or snapcast is None:
        return jsonify({'error': 'TTS or Snapcast not available'}), 503

    def _speak():
        try:
            audio, sr = tts.synth(text)
            snapcast.stream(audio, sr)
        except Exception as e:
            log.error('TTS test failed: %s', e)

    threading.Thread(target=_speak, daemon=True).start()
    return jsonify({'status': 'speaking', 'text': text})


@app.route('/api/sound/test', methods=['POST'])
def sound_test():
    data = request.get_json() or {}
    sound_name = data.get('sound', '').strip()
    if not sound_name:
        return jsonify({'error': 'sound name required'}), 400

    snapcast = _shared.get('snapcast')
    if snapcast is None:
        return jsonify({'error': 'Snapcast not available'}), 503

    def _play():
        try:
            from sound_player import load_sound
            audio, sr = load_sound(sound_name)
            snapcast.stream(audio, sr)
        except Exception as e:
            log.error('Sound test failed: %s', e)

    threading.Thread(target=_play, daemon=True).start()
    return jsonify({'status': 'playing', 'sound': sound_name})


@app.route('/api/snapcast/test', methods=['POST'])
def snapcast_test():
    if _shared.get('snapcast_test_running'):
        return jsonify({'status': 'already running'}), 409

    _shared['snapcast_test_running'] = True

    def _run():
        try:
            test_script = os.path.join(os.path.dirname(__file__), 'snapcast_test.py')
            subprocess.run([sys.executable, test_script], timeout=30)
        except Exception as e:
            log.error('Snapcast test failed: %s', e)
        finally:
            _shared['snapcast_test_running'] = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'status': 'started'})


@app.route('/api/testwavs/sync', methods=['POST'])
def testwavs_sync():
    def _run():
        try:
            sync_script = os.path.join(os.path.dirname(__file__), 'sync_testwavs.sh')
            result = subprocess.run(['bash', sync_script], capture_output=True, text=True, timeout=30)
            log.info('testwavs sync: %s', result.stdout.strip())
        except Exception as e:
            log.error('testwavs sync failed: %s', e)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'status': 'syncing'})


@app.route('/api/pipeline/state')
def pipeline_state():
    return jsonify({'state': _shared.get('pipeline_state', 'IDLE')})


@app.route('/weather/synthesise', methods=['POST'])
def weather_synthesise():
    try:
        payload = flask.request.get_json(force=True)
    except Exception:
        return jsonify({'error': 'invalid_json'}), 400
    if not isinstance(payload, dict) or 'time_series' not in payload:
        return jsonify({'error': 'missing time_series'}), 400
    try:
        from weatherdash import synthesise
        result, status = synthesise(payload)
        return jsonify(result), status
    except Exception as e:
        log.error('Weather synthesis failed: %s', e)
        return jsonify({'error': 'internal_error', 'degraded': True}), 502


def start_web_server(receiver, syncstreamer, tts):
    _shared['receiver'] = receiver
    _shared['syncstreamer'] = syncstreamer
    _shared['tts'] = tts
    _shared['pipeline_state'] = 'IDLE'

    # Suppress Flask's startup banner in production.
    import flask.cli
    flask.cli.show_server_banner = lambda *a: None

    def run():
        app.run(host=WEB_SERVER_HOST, port=WEB_SERVER_PORT, debug=False, use_reloader=False)

    log.info('Starting web server on http://%s:%d', HOST_IP, WEB_SERVER_PORT)
    threading.Thread(target=run, daemon=True).start()
