"""Validated cloud responses, with atomic current/previous files and a local seed."""
import json
import logging
import os
from pathlib import Path
import re
import string
import tempfile
import threading
import urllib.request

log = logging.getLogger(__name__)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def validate_templates(data, required=None):
    if not isinstance(data, dict) or not data:
        raise ValueError('Template response must be a nonempty object')
    result = {}
    for key, values in data.items():
        if not isinstance(key, str) or not isinstance(values, list) or not values:
            raise ValueError('Each template category must be a nonempty list')
        lines = []
        for value in values:
            if not isinstance(value, str) or not 5 <= len(value) <= 400:
                raise ValueError('Invalid template text length')
            fields = list(string.Formatter().parse(value))
            if any(field not in (None, 'device', 'value') or spec or conv
                   for _, field, spec, conv in fields):
                raise ValueError('Only plain {device} and {value} placeholders are allowed')
            lines.append(value)
        result[key] = list(dict.fromkeys(lines))[:8]
    if required and not set(required) <= result.keys():
        raise ValueError('Cloud response is missing requested categories')
    return result


class ResponseCache:
    def __init__(self, directory, seed):
        self.directory = Path(directory)
        self.current = self.directory / 'current.json'
        self.previous = self.directory / 'previous.json'
        self.seed = Path(seed)
        self.lock = threading.RLock()
        self.templates = self._load()

    def _load(self):
        for source in (self.current, self.previous, self.seed):
            try:
                return validate_templates(json.loads(source.read_text()))
            except (OSError, ValueError, TypeError):
                log.warning('Response cache could not load %s', source)
        raise ValueError('No valid response templates available')

    def get(self):
        with self.lock:
            self.templates = self._load()
            return self.templates.copy()

    def update(self, new):
        with self.lock:
            old = self.get()
            new = validate_templates(new, required=old.keys())
            merged = {k: list(dict.fromkeys(new[k] + old[k]))[:8] for k in old}
            # Save the last known-good contents before atomically replacing current.
            atomic_json(self.previous, old)
            atomic_json(self.current, merged)
            self.templates = merged
            return merged

    def refresh(self, device_types):
        import config
        current = self.get()
        prompt = ('Return ONLY a JSON object with these categories: '
                  + ', '.join(current) + '. Each category must have 3 short strings, '
                  'except antics which needs 2. Use a warm, funny Dobby house elf persona. '
                  'At most 20 words per string. Use only {device} and {value} placeholders. '
                  'Device types: ' + ', '.join(sorted(device_types)) + '. '
                  'Return the JSON in your response. Do not use tools, create files, '
                  'or modify the filesystem; the caller stores the result.')
        if not config.CLOUD_LLM_API_KEY:
            raise ValueError('Cloud refresh requires CLOUD_LLM_API_KEY (DEEPSEEK_API_KEY in credentials.py)')
        # A text-only provider request cannot execute agent tools or create stray files.
        request = urllib.request.Request(
            config.CLOUD_LLM_BASE_URL.rstrip('/') + '/chat/completions',
            data=json.dumps({'model': config.CLOUD_LLM_MODEL, 'stream': False,
                             'response_format': {'type': 'json_object'},
                             'max_tokens': config.RESPONSE_REFRESH_MAX_TOKENS,
                             'messages': [{'role': 'user', 'content': prompt}]}).encode(),
            headers={'Content-Type': 'application/json',
                     'Authorization': 'Bearer ' + config.CLOUD_LLM_API_KEY}, method='POST')
        with urllib.request.urlopen(request, timeout=config.RESPONSE_REFRESH_TIMEOUT_SECONDS) as response:
            obj = json.load(response)
        raw = obj['choices'][0]['message']['content'].strip()
        raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw)
        self.update(json.loads(raw))
        log.info('Response cache refreshed through cloud LLM (%d categories)', len(current))
        return True
