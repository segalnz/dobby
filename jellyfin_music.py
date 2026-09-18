import json
import logging
import os
import re
import threading
import time
import urllib.request
import urllib.error

import numpy as np

from syncstreamer import get_syncstreamer, TtsSource, JellyfinSource
from config import (
    JELLYFIN_SERVER_URL,
    JELLYFIN_API_KEY,
    JELLYFIN_SEARCH_LIMIT,
    JELLYFIN_STREAM_CONTAINER,
    JELLYFIN_REQUEST_TIMEOUT_S,
)

log = logging.getLogger(__name__)

def _cfg(name, default=None):
    import importlib
    c = importlib.import_module('config')
    return getattr(c, name, default)

# Voice-to-text translation mappings for fuzzy artist/track names.
# Edit ~/assistant/jellyfin_translations.txt to add entries.
# Format: misheard1, misheard2 = actual (one per line, # comments allowed)
_VOICE_TRANSLATIONS = []
_translations_path = os.path.join(os.path.dirname(__file__), 'jellyfin_translations.txt')
try:
    with open(_translations_path, 'r', encoding='utf-8') as _tf:
        for _line in _tf:
            _line = _line.strip()
            if not _line or _line.startswith('#'):
                continue
            if '=' in _line:
                keys_str, val = _line.split('=', 1)
                keys = [k.strip().lower() for k in keys_str.split(',') if k.strip()]
                if keys and val.strip():
                    _VOICE_TRANSLATIONS.append((keys, val.strip()))
    if _VOICE_TRANSLATIONS:
        log.info('Loaded %d voice translation groups', len(_VOICE_TRANSLATIONS))
except OSError:
    pass

# ── Jellyfin HTTP client ─────────────────────────────────────────────

class JellyfinClient:
    def __init__(self, server_url=JELLYFIN_SERVER_URL, api_key=JELLYFIN_API_KEY):
        self._server = server_url.rstrip('/')
        self._headers = {'X-Emby-Token': api_key}

    def _get(self, path, params=None):
        query = ''
        if params:
            qpairs = []
            for k, v in params.items():
                if isinstance(v, list):
                    for item in v:
                        qpairs.append(f'{k}={urllib.request.quote(str(item))}')
                else:
                    qpairs.append(f'{k}={urllib.request.quote(str(v))}')
            query = '?' + '&'.join(qpairs)
        url = f'{self._server}{path}{query}'
        req = urllib.request.Request(url, headers=self._headers)
        try:
            with urllib.request.urlopen(req, timeout=JELLYFIN_REQUEST_TIMEOUT_S) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            log.error('Jellyfin HTTP %s on %s: %s', e.code, path, e.reason)
            return None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            log.error('Jellyfin request failed %s: %s', path, e)
            return None

    def search(self, query, item_types=None):
        if item_types is None:
            item_types = ['Audio', 'MusicAlbum', 'MusicArtist', 'Playlist']
        res = self._get('/Items', {
            'searchTerm': query,
            'includeItemTypes': ','.join(item_types),
            'recursive': 'true',
            'limit': str(JELLYFIN_SEARCH_LIMIT),
            'sortBy': 'SortName',
            'sortOrder': 'Ascending',
        })
        if not res:
            return []
        return res.get('Items', [])

    def get_album_tracks(self, album_id):
        res = self._get('/Items', {
            'parentId': album_id,
            'includeItemTypes': 'Audio',
            'sortBy': 'ParentIndexNumber,IndexNumber',
            'sortOrder': 'Ascending',
        })
        if not res:
            return []
        return res.get('Items', [])

    def get_artist_content(self, artist_id):
        tracks = self._get('/Items', {
            'artistIds': artist_id,
            'includeItemTypes': 'Audio',
            'recursive': 'true',
            'sortBy': 'Album,ParentIndexNumber,IndexNumber',
            'sortOrder': 'Ascending',
            'limit': '200',
        })
        return tracks.get('Items', []) if tracks else []

    def get_playlist_items(self, playlist_id):
        res = self._get(f'/Playlists/{playlist_id}/Items', {
            'includeItemTypes': 'Audio',
        })
        if not res:
            return []
        return res.get('Items', [])

    def get_stream_url(self, item_id):
        return f'{self._server}/Audio/{item_id}/stream.ogg'

    def ping(self):
        res = self._get('/System/Info/Public')
        return res is not None


# ── Music controller ─────────────────────────────────────────────────

class MusicController:
    def __init__(self):
        self._jellyfin = JellyfinClient()
        self._syncer = None
        self._tts = None
        self._lock = threading.RLock()  # reentrant — handle_action→advance chain
        self._cancel_event = threading.Event()
        self._playback_gen = 0       # incremented on cancel — old threads check this
        self._playback_thread = None
        self.queue = []
        self.queue_index = -1
        self.state = 'idle'  # idle | playing | paused | voice_interrupt
        self._current_track = None
        self._interrupted_track_idx = -1  # track to resume after voice command
        self._artist_names_cache = None       # cached Jellyfin artist list

    def _set_syncer(self, syncer):
        self._syncer = syncer

    def _set_tts(self, tts):
        self._tts = tts

    def interrupt(self):
        """Stop playback for voice command — will resume afterwards."""
        with self._lock:
            if self.state != 'playing' or self.queue_index < 0:
                return False
            self._cancel_playback()
            self._interrupted_track_idx = self.queue_index
            self.state = 'voice_interrupt'
            log.info('Music interrupted for voice command (track %d/%d)',
                     self.queue_index, len(self.queue))
            return True

    def resume_after_interrupt(self):
        """Resume playback after voice command completes."""
        with self._lock:
            if self.state != 'voice_interrupt' or self._interrupted_track_idx < 0:
                return False
            idx = self._interrupted_track_idx
            if idx >= len(self.queue):
                self._interrupted_track_idx = -1
                return False
            self._interrupted_track_idx = -1
            self.queue_index = idx
            self._start_track(self.queue[idx])
            log.info('Music resumed after voice interrupt: %s',
                     self.queue[idx].get('Name', '?'))
            return True

    def _speak(self, text):
        if self._tts is not None:
            try:
                audio, sr = self._tts.synth(text)
                if self._syncer is not None:
                    source = TtsSource(audio, sr, gain=0.42)
                    self._syncer.play_tts(source)
            except Exception:
                log.warning('Music TTS feedback failed', exc_info=True)

    def _select_best_match(self, items, query, prefer_type=None):
        if not items:
            return None, []
        if prefer_type:
            typed = [i for i in items if i.get('Type') == prefer_type]
            candidates = typed if typed else items
        else:
            candidates = items
        return candidates[0], candidates

    # ── command parsing ─────────────────────────────────────────────

    _MUSIC_PATTERNS = re.compile(
        r'(?:(?:ask\s+)?jelly\s*fin\s+to\s+)?'
        r'(?P<action>what\s*(?:\'s|is)\s*(?:now\s*)?play\S*|'
        r'what\s*(?:\'s|is)\s*this|'
        r'now\s*play\S*|'
        r'play|put\s+on|start|pause|stop|resume|next|previous|prev|skip|back)'
        r'(?:\s+(?P<type>album|track|song|playlist|artist|band|music)\b)?'
        r'(?:\s+(?P<query>.+?))?'
        r'[.]?$',
        re.IGNORECASE,
    )

    _JELLYFIN_MENTION = re.compile(r'jelly\s*fin', re.IGNORECASE)

    _STANDALONE_PATTERNS = re.compile(
        r'^(?P<action>pause|stop|resume|next|previous|prev|back|skip|'
        r"what\s*(?:'s|is)\s*(?:now\s*)?play\S*|"
        r"what\s*(?:'s|is)\s*this|"
        r"now\s*play\S*)"
        r'(?:\s+(?:the\s+)?(?:music|playback|song|track))?'
        r'[.]?$',
        re.IGNORECASE,
    )

    def is_music_command(self, text):
        t = text.strip().lstrip('-\u0022\u0027*#').rstrip('.?!,;:')
        # Any jellyfin mention
        if self._JELLYFIN_MENTION.search(t):
            return True
        if self._STANDALONE_PATTERNS.match(t):
            return True
        m = self._MUSIC_PATTERNS.search(t)
        if m and m.group('action'):
            q = m.group('query')
            action = m.group('action').lower()
            if q:
                return True
            # Play/pause/stop without query — only when actively playing
            if action in ('play', 'pause', 'stop', 'resume', 'next', 'previous', 'prev', 'skip', 'back', 'puton', 'start'):
                return self.state != 'idle'
            # "what is playing" etc always valid
            if action in ('whatsplaying', 'whatisplaying', 'whatisthis', 'nowplaying'):
                return True
        if self.state != 'idle':
            if m and m.group('action'):
                return True
        return False

    # ── main entry ──────────────────────────────────────────────────

    def handle_command(self, text):
        import re
        text = re.sub(r'\s+', ' ', text.replace(",", " ").replace("\n", " ")).strip().lstrip('-\u0022\u0027*#').rstrip('.?!,;:')
        m_standalone = self._STANDALONE_PATTERNS.match(text)
        if m_standalone:
            action = m_standalone.group('action').lower()
            try: scope = (m_standalone.group('scope') or '').lower()
            except IndexError: scope = ''
            return self._handle_playback_action(action, scope)

        m = self._MUSIC_PATTERNS.search(text)
        if not m:
            return None

        action = (m.group('action') or '').lower().replace(' ', '')
        if action in ('whatsplaying', 'whatisplaying', 'whatisthis', 'nowplaying'):
            return self._handle_now_playing()

        item_type = (m.group('type') or '').lower()
        query = (m.group('query') or '').strip()

        if action in ('pause', 'stop', 'resume', 'next', 'previous', 'prev', 'skip', 'back'):
            album_level = 'album' in (item_type or '') or 'album' in (query or '').lower()
            return self._handle_playback_action(action, 'album' if album_level else '')

        if action not in ('play', 'puton', 'start'):
            return None

        if not query:
            if self.state == 'paused':
                return self._handle_playback_action('resume', None)
            return ('prompt', 'What would you like me to play?')

        return self._handle_play_query(query, item_type)

    # ── playback actions ────────────────────────────────────────────

    def _handle_playback_action(self, action, query):
        with self._lock:
            album_level = 'album' in (query or '').lower()
            if action in ('back', 'previous', 'prev'):
                return self._advance(-1, album_level)
            elif action in ('next', 'skip'):
                return self._advance(1, album_level)
            elif action == 'pause':
                if self.state == 'playing':
                    self._cancel_playback()
                    self.state = 'paused'
                    return ('paused', f'Paused {self._track_label_locked(self._current_track)}')
                elif self.state == 'idle':
                    return ('idle', 'Nothing is playing right now.')
            elif action == 'resume':
                if self.state == 'paused':
                    self._start_track(self.queue[self.queue_index])
                    return ('playing', 'Resumed.')
                elif self.state == 'idle':
                    return ('idle', 'Nothing to resume.')
            elif action == 'stop':
                self._cancel_playback()
                self.queue = []
                self.queue_index = -1
                self.state = 'idle'
                return ('idle', 'Music stopped.')
        return None

    def _handle_play_query(self, query, item_type):
        if not self._jellyfin.ping():
            self._speak("I can't reach the music server right now.")
            return ('error', "I can't reach the music server right now.")

        item_type = item_type or ''
        if item_type in ('album',):
            return self._play_album(query)
        elif item_type in ('track', 'song', 'music'):
            return self._play_track(query)
        elif item_type in ('playlist',):
            return self._play_playlist(query)
        elif item_type in ('artist', 'band', 'the'):
            return self._play_artist(query)
        elif item_type:
            return None  # unknown type, fall through to intent parser

        cleaned = self._translate_query(self._clean_query(query))
        items = self._jellyfin.search(cleaned)
        if not items:
            # Try difflib match against library artist names
            match = self._resolve_from_library(query)
            if match:
                log.info('Generic search %r → difflib matched: %s', query, match)
                items = self._jellyfin.search(match)
            if not items:
                self._speak(f"I couldn't find anything for {query}.")
                return ('empty', f"No results for '{query}'.")

        # When no type specified, prefer artist over track (e.g. "play pink" → P!nk)
        first = items[0]
        item_type = first.get('Type', '')
        if item_type == 'Audio':
            artists_in_results = [i for i in items if i.get('Type') == 'MusicArtist']
            if artists_in_results:
                return self._play_artist(query)

        if item_type == 'MusicArtist':
            return self._play_artist_from_item(first)
        elif item_type == 'MusicAlbum':
            return self._play_album_tracks(first)
        elif item_type == 'Audio':
            return self._play_track_direct(first)
        elif item_type == 'Playlist':
            return self._play_playlist_direct(first)
        else:
            return self._play_artist(query)

    def _handle_now_playing(self):
        with self._lock:
            if self.state in ('playing', 'paused') and self._current_track:
                label = self._track_label_locked(self._current_track)
                return ('now_playing', f"Now playing: {label}")
            return ('idle', 'Nothing is playing right now.')

    # ── play methods ───────────────────────────────────────────────

    def _play_artist(self, query):
        query = self._clean_query(query)
        query = self._translate_query(query)
        items = self._jellyfin.search(query, ['MusicArtist'])

        qn = self._norm_name(query)
        exact = [i for i in items if i.get('Name', '').lower() == query]
        fuzzy = [i for i in items
                 if self._norm_name(i.get('Name', '')) == qn
                 or self._norm_name(i.get('Name', '')).lstrip('the') == qn]

        # Try variant searches first — they found P!nk from "pink" before
        if not exact and not fuzzy:
            for vq in self._query_variants(query):
                vitems = self._jellyfin.search(self._translate_query(vq), ['MusicArtist'])
                if vitems:
                    v_exact = [i for i in vitems if i.get('Name', '').lower() == vq]
                    v_fuzzy = [i for i in vitems
                               if self._norm_name(i.get('Name', '')) == qn]
                    if v_exact or v_fuzzy:
                        log.info('Artist search %r → variant %r matched: %s',
                                 query, vq, [i.get('Name') for i in vitems])
                        items = vitems
                        exact = v_exact
                        fuzzy = v_fuzzy
                        break

        # LLM resolution for ambiguous multi-candidate results
        if len(items) > 1 and self._llm_available():
            best = self._resolve_artist_with_llm(query, items)
            if best:
                log.info('Artist search %r → LLM resolved: %s from %d candidates',
                         query, best.get('Name'), len(items))
                return self._play_artist_from_item(best)

        if not items:
            match = self._resolve_from_library(query)
            if match:
                log.info('Artist search %r → difflib matched from library: %s', query, match)
                items = self._jellyfin.search(match, ['MusicArtist'])
                if items:
                    exact = [i for i in items if i.get('Name', '').lower() == match.lower()]
                    fuzzy = [i for i in items
                             if self._norm_name(i.get('Name', '')) == self._norm_name(match)]
            if not items:
                self._speak(f"I couldn't find an artist named {query}.")
                return ('empty', f"No artist: {query}.")

        # Prefer exact match, then fuzzy, then first result
        best = (exact or fuzzy or items)[0]
        names = [i.get('Name', '?') for i in items[:5]]
        log.info('Artist search %r → found: %s → selected: %s', query, names, best.get('Name'))
        return self._play_artist_from_item(best)

    @staticmethod
    def _llm_available() -> bool:
        try:
            from config import OLLAMA_BASE_URL
            return bool(OLLAMA_BASE_URL)
        except Exception:
            return False

    _artist_names_cache = None

    def _resolve_from_library(self, query: str) -> str | None:
        """Match query against actual artist list using string similarity."""
        artist_names = self._get_artist_names()
        if not artist_names:
            return None
        try:
            import difflib
            q = query.lower()
            targets = {n.strip(): n.strip() for n in artist_names}
            target_keys = [k.lower() for k in targets]

            # 1. Try exact match (case-insensitive)
            for k, v in zip(target_keys, targets.keys()):
                if q == k:
                    return targets[v]

            # 2. Try difflib fuzzy match
            matches = difflib.get_close_matches(q, target_keys, n=3, cutoff=0.5)
            if matches:
                for k, v in zip(target_keys, targets.keys()):
                    if k == matches[0]:
                        return targets[v]

            # 3. Try substring match (e.g. "seer" doesn't match "sia" but
            #    at least catches partial names)
            for k, v in zip(target_keys, targets.keys()):
                if len(q) >= 3 and q in k:
                    return targets[v]

            return None
        except Exception:
            log.debug('difflib match failed', exc_info=True)
            return None

    def _get_artist_names(self) -> list:
        """Fetch all MusicArtist names from Jellyfin (cached)."""
        if self._artist_names_cache is not None:
            return self._artist_names_cache
        try:
            items = []
            start = 0
            while True:
                res = self._jellyfin._get('/Items', {
                    'includeItemTypes': 'MusicArtist',
                    'recursive': 'true',
                    'startIndex': str(start),
                    'limit': '200',
                    'sortBy': 'SortName',
                })
                batch = res.get('Items', []) if res else []
                if not batch:
                    break
                items.extend(i.get('Name', '') for i in batch)
                if len(batch) < 200:
                    break
                start += 200
            self._artist_names_cache = sorted(set(items))
            log.info('Loaded %d artist names from Jellyfin', len(self._artist_names_cache))
            return self._artist_names_cache
        except Exception:
            log.warning('Failed to load artist names from Jellyfin')
            return []

    def _resolve_from_library(self, query: str) -> str | None:
        """Match query against Jellyfin artist list via difflib string similarity."""
        artist_names = self._get_artist_names()
        if not artist_names:
            return None
        try:
            import difflib
            q = query.lower()
            targets = {n.strip().lower(): n.strip() for n in artist_names if n.strip()}
            # difflib with 0.75 cutoff — catches 'busch'/'bush' but not phonetic errors
            matches = difflib.get_close_matches(q, list(targets.keys()), n=1, cutoff=0.75)
            if matches:
                return targets[matches[0]]
            return None
        except Exception:
            return None

    def _play_artist_with_name(self, name: str):
        """Play artist by exact name match."""
        items = self._jellyfin.search(name, ['MusicArtist'])
        if not items:
            return ('empty', f'No artist: {name}')
        return self._play_artist_from_item(items[0])

    def _resolve_artist_with_llm(self, query: str,
                                  candidates: list) -> dict | None:
        try:
            from config import LOCAL_INTENT_MODEL, OLLAMA_BASE_URL, OLLAMA_KEEP_ALIVE
            import urllib.request
            candidate_desc = ', '.join(
                f'{c.get("Name", "?")} ({c.get("Id","")[:8]})'
                for c in candidates[:5]
            )
            prompt = (
                f'A user said "{query}" meaning a music artist. '
                f'Jellyfin search results: [{candidate_desc}]. '
                f'Resolve to the single CORRECT artist. '
                f'Output ONLY the exact artist name from the list (or "none").'
            )
            payload = json.dumps({
                "model": LOCAL_INTENT_MODEL,
                "prompt": prompt,
                "format": "json",
                "stream": False,
                "keep_alive": OLLAMA_KEEP_ALIVE,
                "options": {"temperature": 0.0, "num_predict": 64},
            }).encode()
            req = urllib.request.Request(
                f'{OLLAMA_BASE_URL}/api/generate',
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                obj = json.loads(resp.read().decode())
            result = json.loads(obj.get("response", "{}"))
            name = result.get('artist', result.get('name', result.get('response',
                              obj.get('response', '')))).strip().strip('"')
            if not name or name.lower() == 'none':
                return None
            # Find the matching candidate
            for c in candidates:
                if c.get('Name', '').lower() == name.lower():
                    return c
            return None
        except Exception:
            log.debug('LLM artist resolution failed', exc_info=True)
            return None

    def _dedupe_artists(self, items: list) -> list:
        """Merge artist entries that differ only by leading 'The ' prefix."""
        if len(items) <= 1:
            return items
        # Build groups keyed by name without leading "the "
        groups = {}
        for item in items:
            name = item.get('Name', '').lower()
            key = re.sub(r'^the\s+', '', name)
            groups.setdefault(key, []).append(item)
        # For each group, pick the one with the most tracks (or longest name as tiebreaker)
        merged = []
        for items in groups.values():
            if len(items) == 1:
                merged.append(items[0])
            else:
                best = None
                best_count = -1
                for item in items:
                    tracks = self._jellyfin.get_artist_content(item['Id'])
                    count = len(tracks)
                    if count > best_count:
                        best_count = count
                        best = item
                if best:
                    merged.append(best)
        return merged if merged else items

    @staticmethod
    def _translate_query(query: str) -> str:
        """Apply voice translation mappings: 'cause'/'because' → 'corrs'."""
        result = query.lower()
        for keys, actual in _VOICE_TRANSLATIONS:
            for misheard in keys:
                result = re.sub(rf'\b{re.escape(misheard)}\b', actual, result)
        return result

    @staticmethod
    def _norm_name(s: str) -> str:
        return ''.join(c.lower() for c in s if c.isalnum())

    @staticmethod
    def _speakable_name(name: str) -> str:
        """Strip stylized characters for natural TTS: P!nk→Pink, AC/DC→ACDC."""
        mapping = {'!': 'i', '@': 'a', '3': 'e', '$': 's', '/': ' ', '.': ' ',
                   '#': '', '&': 'and', '*': '', '_': ' '}
        out = ''.join(mapping.get(c, c) for c in name)
        return ' '.join(out.split())  # collapse whitespace

    @staticmethod
    def _query_variants(query: str) -> list[str]:
        """Try common stylized-name substitutions (P!nk→Pink, AC/DC→ACDC)."""
        subs = [('!', 'i'), ('@', 'a'), ('3', 'e'), ('$', 's'),
                ('/', ' '), ('.', ' '), ('&', 'and')]
        seen = {query}
        out = []
        for a, b in subs:
            if a and a in query:
                v = query.replace(a, b)
                if v not in seen:
                    seen.add(v); out.append(v)
            if b and b in query:
                v = query.replace(b, a)
                if v not in seen:
                    seen.add(v); out.append(v)
        return out

    @staticmethod
    def _clean_query(query: str) -> str:
        return query.strip().replace(chr(34), '').replace(chr(39), '').rstrip('.!?,;:')

    def _play_artist_from_item(self, artist_item):
        tracks = self._jellyfin.get_artist_content(artist_item['Id'])
        if not tracks:
            first_album = artist_item.get('Name', 'Unknown')
            self._speak(f"I found {first_album} but couldn't load any tracks.")
            return ('empty', 'No tracks available.')
        self._set_queue(tracks, 0)
        name = self._speakable_name(artist_item.get('Name', 'artist'))
        self._speak(f"Playing {name}.")
        self._start_track(tracks[0])
        return ('playing', f"Playing artist {name}")

    def _play_album(self, query):
        query = self._clean_query(query)
        query = self._translate_query(query)
        items = self._jellyfin.search(query, ['MusicAlbum'])
        if not items:
            self._speak(f"I couldn't find an album named {query}.")
            return ('empty', f"No album: {query}.")
        return self._play_album_tracks(items[0])

    def _play_album_tracks(self, album_item):
        tracks = self._jellyfin.get_album_tracks(album_item['Id'])
        if not tracks:
            self._speak(f"I found the album but couldn't load its tracks.")
            return ('empty', 'No tracks.')
        for t in tracks:
            t['Album'] = album_item.get('Name', '')
        self._set_queue(tracks, 0)
        name = self._speakable_name(album_item.get('Name', ''))
        self._speak(f"Playing album {name}.")
        self._start_track(tracks[0])
        return ('playing', f"Playing album {name}")

    def _play_track(self, query):
        query = self._clean_query(query)
        query = self._translate_query(query)
        items = self._jellyfin.search(query, ['Audio'])
        if not items:
            self._speak(f"I couldn't find a track named {query}.")
            return ('empty', f"No track: {query}.")
        return self._play_track_direct(items[0])

    def _play_track_direct(self, track_item):
        self._set_queue([track_item], 0)
        name = self._speakable_name(track_item.get('Name', 'track'))
        self._speak(f"Playing {name}.")
        self._start_track(track_item)
        return ('playing', f"Playing track {name}")

    def _play_playlist(self, query):
        query = self._clean_query(query)
        query = self._translate_query(query)
        items = self._jellyfin.search(query, ['Playlist'])
        if not items:
            self._speak(f"I couldn't find a playlist named {query}.")
            return ('empty', f"No playlist: {query}.")
        return self._play_playlist_direct(items[0])

    def _play_playlist_direct(self, playlist_item):
        tracks = self._jellyfin.get_playlist_items(playlist_item['Id'])
        if not tracks:
            self._speak("This playlist is empty.")
            return ('empty', 'Empty playlist.')
        self._set_queue(tracks, 0)
        name = self._speakable_name(playlist_item.get('Name', ''))
        self._speak(f"Playing playlist {name}.")
        self._start_track(tracks[0])
        return ('playing', f"Playing playlist {name}")

    # ── queue management ────────────────────────────────────────────

    def _set_queue(self, tracks, index):
        with self._lock:
            self._cancel_playback()
            self.queue = tracks
            self.queue_index = max(0, min(index, len(tracks) - 1)) if tracks else -1

    def _advance(self, delta, album_level=False):
        with self._lock:
            if not self.queue:
                log.info('_advance(%d): queue empty', delta)
                return ('idle', 'Nothing is playing.')
            if album_level:
                return self._album_advance(delta)
            return self._track_advance(delta)

    def _track_advance(self, delta, cancel_current=True):
        new_idx = self.queue_index + delta
        if new_idx < 0 or new_idx >= len(self.queue):
            if delta < 0:
                log.info('_track_advance(%d): already at first track', delta)
                return ('playing', 'Already at the first track.')
            self.state = 'idle'
            self.queue = []
            self.queue_index = -1
            log.info('_track_advance(%d): end of queue (%d tracks), stopping', delta, len(self.queue))
            return ('idle', 'End of queue.')
        old_track = self.queue[self.queue_index].get('Name', '?') if 0 <= self.queue_index < len(self.queue) else '?'
        next_track = self.queue[new_idx].get('Name', '?')
        # Only cancel when skipping manually (next/prev command).
        # Auto-advance after natural track end doesn't need cancel — and
        # snapcast.cancel() sets the shared event that can block the next track.
        if cancel_current:
            self._cancel_playback()
        self.queue_index = new_idx
        log.info('_track_advance(%d): %d/%d %r → %r', delta, new_idx, len(self.queue), old_track, next_track)
        track = self.queue[new_idx]
        label = self._track_label_locked(track)
        self._start_track(track)
        return ('playing', f'Playing {label}')

    def _album_advance(self, delta):
        if self.queue_index < 0 or self.queue_index >= len(self.queue):
            return ('idle', 'Nothing is playing.')
        cur_album = self.queue[self.queue_index].get('AlbumId', '') or ''
        direction = 1 if delta >= 0 else -1
        i = self.queue_index + direction
        while 0 <= i < len(self.queue):
            aid = self.queue[i].get('AlbumId', '') or ''
            if aid and aid != cur_album:
                self._cancel_playback()
                self.queue_index = i
                track = self.queue[i]
                album_name = self._speakable_name(track.get('Album', 'next album'))
                log.info('_album_advance(%d): %d/%d → album %r', delta, i, len(self.queue), album_name)
                label = self._track_label_locked(track)
                self._start_track(track)
                return ('playing', f'{album_name} — {label}')
            i += direction
        return ('idle', 'No more albums.')

    def _track_label(self):
        with self._lock:
            if self.queue_index < 0 or self.queue_index >= len(self.queue):
                return ''
            return self._track_label_locked(self.queue[self.queue_index])

    def _track_label_locked(self, track):
        name = self._speakable_name(track.get('Name', 'Unknown'))
        artists = track.get('Artists', []) or track.get('ArtistItems', [])
        album = track.get('Album', '')
        if artists:
            artist_str = artists[0] if isinstance(artists[0], str) else artists[0].get('Name', '')
            artist_str = self._speakable_name(artist_str)
            label = f'{name} by {artist_str}'
        else:
            label = name
        if album:
            album = self._speakable_name(album)
            label += f' from {album}'
        return label

    # ── audio playback ──────────────────────────────────────────────

    def _start_track(self, track):
        log.info('_start_track: %s', track.get('Name', '?'))
        self._current_track = track
        self.state = 'playing'
        self._playback_thread = threading.Thread(
            target=self._stream_worker, args=(track,), daemon=True
        )
        self._playback_thread.start()

    def _stream_worker(self, track):

        import io

        tname = track.get('Name', '?')
        my_gen = self._playback_gen  # snapshot — only advance if still current
        self._cancel_event.clear()
        log.info('_stream_worker start: %s (gen=%d)', tname, my_gen)

        try:
            import soundfile as sf
        except ImportError:
            log.error('soundfile not available — streaming not possible')
            return

        url = self._jellyfin.get_stream_url(track['Id'])
        log.info('Jellyfin stream: %s', url)
        try:
            req = urllib.request.Request(url, headers=self._jellyfin._headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
            buf = io.BytesIO(raw)
            data, sr = sf.read(buf, dtype='float32')
        except Exception:
            log.error('Failed to stream track %s', tname, exc_info=True)
            if not self._cancel_event.is_set():
                self._track_advance(1, cancel_current=False)
            return

        ch = data.shape[1] if data.ndim > 1 else 1
        data = data.astype(np.float32)
        syncstreamer = self._syncer or get_syncstreamer()

        if syncstreamer is not None and not self._cancel_event.is_set():
            source = JellyfinSource(data, sr,
                                   gain=0.42, channels=ch)
            syncstreamer.play_music(source)
            log.info('Streamed track: %s (%.1fs %dch)', tname, len(data) / sr, ch)
        else:
            log.info('Skipping stream: syncer=%s cancel=%s',
                     syncstreamer is not None, self._cancel_event.is_set())

        if my_gen == self._playback_gen and not self._cancel_event.is_set():
            log.info('_stream_worker auto-advancing from %s (gen=%d)', tname, my_gen)
            time.sleep(0.3)
            log.info('_stream_worker calling _advance from %s', tname)
            result = self._track_advance(1, cancel_current=False)
            log.info('_stream_worker _advance returned: %s', result)
        else:
            log.info('_stream_worker stale after %s (gen=%d now=%d)', tname, my_gen, self._playback_gen)

    def _cancel_playback(self):
        self._cancel_event.set()
        self._playback_gen += 1
        if self._syncer is not None:
            self._syncer.stop_music()
        # Don't join — blocks wakeword processing. Thread exits naturally.
        self._playback_thread = None


# ── singleton ────────────────────────────────────────────────────────

_music_controller = None


def init_music_controller(syncer=None, tts=None):
    global _music_controller
    if _music_controller is None:
        _music_controller = MusicController()
    if syncer is not None:
        _music_controller._set_syncer(syncer)
    if tts is not None:
        _music_controller._set_tts(tts)
    return _music_controller


def get_music_controller():
    global _music_controller
    if _music_controller is None:
        _music_controller = MusicController()
    return _music_controller
