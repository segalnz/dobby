import threading
import time
import socket
import logging

from syncstreamer.config import SyncStreamerConfig
from syncstreamer.client_registry import ClientRegistry
from syncstreamer.protocol import pack, FRAMES_PER_PACKET, SAMPLE_RATE

log = logging.getLogger(__name__)

AUDIO_PORT = 5005


class SyncStreamerModule:
    def __init__(self, config: SyncStreamerConfig):
        self._cfg = config
        self._registry = ClientRegistry(config.announce_port, config.client_ttl_sec)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 131072)
        self._stop = threading.Event()
        self._send_thread = None
        self._muted = False  # suppress audio during voice capture
        self._tts_active = False  # True while TTS is streaming
        self._muted = False  # suppress audio during voice capture
        self._tts_active = False  # True while TTS is streaming

    @property
    def state(self):
        return "idle"

    def play_music(self, source):
        self._muted = False
        log.info("MUTE: music force-unmuted on play_music")
        self._stream(source, channels=2)

    def play_tts(self, source):
        was = self._muted
        self._tts_active = True
        self._muted = True   # silence music during TTS
        try:
            self._stream(source, channels=1)
        finally:
            self._muted = was
            self._tts_active = False

    def stop_music(self):
        self._stop.set()

    def on_mic_open(self):
        self._muted = True
        log.info("MUTE: music suppressed (on_mic_open)")   # suppress music during speech

    def on_mic_close(self):
        self._muted = False
        log.info("MUTE: music resumed (on_mic_close)")  # resume music after speech


    def send_ready_ping(self):
        """Send a stream_id=0 marker on port 5005 signalling server is idle."""
        from syncstreamer.protocol import pack
        pkt = pack(0, 0, bytes(1024))  # stream_id=0 = idle marker
        clients = self._registry.active_clients()
        for ip in clients:
            try:
                self._sock.sendto(pkt, (ip, AUDIO_PORT))
            except OSError:
                pass

    def _stream(self, source, channels):
        self._stop.clear()
        self._registry.send_control("STREAM_START")

        stream_start_us = int(time.time() * 1e6)
        seq = 0
        frame_idx = 0
        interval_s = FRAMES_PER_PACKET / SAMPLE_RATE

        try:
            next_send = time.perf_counter()
            for block_bytes in source.generate_blocks():
                if self._stop.is_set():
                    break
                present_us = (stream_start_us
                              + (frame_idx * 1_000_000) // SAMPLE_RATE
                              + self._cfg.server_lead_us)
                seq_flags = seq | 0x80000000 if channels == 1 else seq
                pkt = pack(seq_flags, present_us, block_bytes)
                if channels == 1 or not self._muted:  # TTS always sends, music obeys mute
                    clients = self._registry.active_clients()
                    for ip in clients:
                        try:
                            self._sock.sendto(pkt, (ip, AUDIO_PORT))
                        except OSError:
                            pass
                seq += 1
                frame_idx += FRAMES_PER_PACKET
                next_send += interval_s
                sleep = next_send - time.perf_counter()
                if sleep > 0:
                    time.sleep(sleep)
        except Exception:
            log.error("Stream send failed", exc_info=True)
        finally:
            self._registry.send_control("STREAM_STOP")


_syncstreamer = None


def init_syncstreamer(config=None):
    global _syncstreamer
    if config is None:
        config = SyncStreamerConfig()
    _syncstreamer = SyncStreamerModule(config)
    return _syncstreamer


def get_syncstreamer():
    global _syncstreamer
    if _syncstreamer is None:
        _syncstreamer = SyncStreamerModule(SyncStreamerConfig())
    return _syncstreamer
