import logging
import socket
import threading
import time

from config import (
    BARGE_IN_ESP32_IPS,
    BARGE_IN_MUTE_PORT,
    ENABLE_BARGE_IN,
)

log = logging.getLogger(__name__)


class BargeInController:
    def __init__(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._last_command = 0x00
        self._lock = threading.Lock()
        self.cancel_event = threading.Event()

    def duck(self):
        with self._lock:
            if self._last_command == 0x01:
                return
            self._last_command = 0x01
            for ip in BARGE_IN_ESP32_IPS:
                try:
                    self._sock.sendto(b'\x01', (ip, BARGE_IN_MUTE_PORT))
                    log.info('Barge-in duck → %s:%d', ip, BARGE_IN_MUTE_PORT)
                except Exception as e:
                    log.warning('Barge-in duck failed for %s: %s', ip, e)
        self.cancel_event.set()

    def restore(self):
        with self._lock:
            if self._last_command == 0x00:
                return
            self._last_command = 0x00
            for ip in BARGE_IN_ESP32_IPS:
                for i in range(3):
                    try:
                        self._sock.sendto(b'\x00', (ip, BARGE_IN_MUTE_PORT))
                        log.info('Barge-in restore → %s:%d  [%d/3]', ip, BARGE_IN_MUTE_PORT, i + 1)
                    except Exception as e:
                        log.warning('Barge-in restore failed for %s: %s', ip, e)
                    if i < 2:
                        time.sleep(0.05)
        self.cancel_event.clear()


_barge_in_singleton = None


def get_barge_in():
    global _barge_in_singleton
    if ENABLE_BARGE_IN and _barge_in_singleton is None:
        _barge_in_singleton = BargeInController()
    return _barge_in_singleton
