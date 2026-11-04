"""Generic client for a service's Unix-domain-socket "feed" (see
drive/feed.py's DriveFeedServer, oxts-nav/nav_feed.py's NavFeedServer for
the server side): connects, reads the 4-byte-big-endian-length-prefixed
pickled-dict framing, and reconnects automatically (1s backoff) if the
owning service hasn't started yet or restarts.

First built as aruco's own nav_client.py (the first consumer of
nav_feed.py); promoted here once navigate needed the same thing for both
oxts-nav's and drive's feeds — see the shared-asset-promotion convention
in ui-style.md ("build where first needed, promote once a second consumer
needs it").
"""

import logging
import pickle
import socket
import struct
import threading
import time


class FeedClient:
    def __init__(self, socket_path, default=None):
        self.socket_path = socket_path
        self.lock = threading.Lock()
        self._latest = default if default is not None else {}

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def latest(self) -> dict:
        with self.lock:
            return self._latest

    def _run(self):
        while True:
            try:
                conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                conn.connect(self.socket_path)
                self._read_loop(conn)
            except OSError:
                time.sleep(1.0)  # owning service not up yet, or restarting — keep retrying

    def _read_loop(self, conn):
        try:
            while True:
                (length,) = struct.unpack(">I", self._recv_exact(conn, 4))
                payload = pickle.loads(self._recv_exact(conn, length))
                with self.lock:
                    self._latest = payload
        except (OSError, EOFError):
            logging.warning("[feed_client] Lost connection to %s, reconnecting", self.socket_path)
        finally:
            conn.close()

    @staticmethod
    def _recv_exact(conn, n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise EOFError("feed connection closed")
            buf += chunk
        return buf
