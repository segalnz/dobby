import socket
import threading
import time
import logging

log = logging.getLogger(__name__)

ANNOUNCE_PORT = 5006
CLIENT_TTL_S  = 90.0


class ClientRegistry:
    """Unified: SYNC_HELLO listener + control sender on single UDP socket port 5006."""

    def __init__(self, announce_port=ANNOUNCE_PORT, ttl_sec=CLIENT_TTL_S):
        self._clients = {}
        self._lock = threading.Lock()
        self._ttl = ttl_sec
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("", announce_port))
        self._port = announce_port
        self._thread = threading.Thread(target=self._listen, daemon=True)
        self._thread.start()
        log.info("ClientRegistry listening on UDP port %d", announce_port)

    def _listen(self):
        while True:
            try:
                data, (ip, _) = self._sock.recvfrom(64)
                if data[:10] == b"SYNC_HELLO":
                    with self._lock:
                        self._clients[ip] = time.monotonic()
                        log.info("SYNC_HELLO from %s (%d clients)", ip, len(self._clients))
            except OSError:
                break

    def active_clients(self):
        now = time.monotonic()
        with self._lock:
            return [ip for ip, t in self._clients.items() if now - t < self._ttl]

    def count(self):
        return len(self.active_clients())

    def send_control(self, msg: str):
        """Send control message to all active clients on port 5006."""
        payload = msg.encode()
        for ip in self.active_clients():
            try:
                self._sock.sendto(payload, (ip, self._port))
            except OSError:
                pass

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass
