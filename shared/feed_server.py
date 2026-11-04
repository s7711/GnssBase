"""Generic Unix-domain-socket "feed" server: publishes whatever
`snapshot_fn()` returns to any number of connected clients, each at its
own send loop, `hz` times a second.

Protocol: any number of clients may connect. Each gets its own send loop.
Every message is a 4-byte big-endian length prefix followed by that many
bytes of a pickled dict — see shared/feed_client.py for the matching
client. A stalled/dead client's loop exits independently — it never
blocks or backs up delivery to other clients.

First built as drive/feed.py's DriveFeedServer (itself already this
generic — snapshot_fn was its constructor parameter from the start);
promoted here once navigate needed the same thing for its own feed,
alongside oxts-nav's NavFeedServer (which wraps this with its own
snapshot_fn closure — see oxts-nav/nav_feed.py) — see the shared-asset-
promotion convention in ui-style.md.
"""

import logging
import os
import pickle
import socket
import struct
import threading
import time


class FeedServer:
    def __init__(self, socket_path, snapshot_fn, hz):
        self.socket_path = socket_path
        self.snapshot_fn = snapshot_fn
        self.period = 1.0 / hz

    def start(self):
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self):
        if os.path.exists(self.socket_path):
            os.remove(self.socket_path)  # Stale socket from a crashed/killed previous run
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self.socket_path)
        server.listen(5)
        logging.info("[feed_server] Listening on %s", self.socket_path)
        while True:
            conn, _ = server.accept()
            threading.Thread(target=self._client_loop, args=(conn,), daemon=True).start()

    def _client_loop(self, conn):
        try:
            while True:
                data = pickle.dumps(self.snapshot_fn())
                conn.sendall(struct.pack(">I", len(data)) + data)
                time.sleep(self.period)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # Client gone — that client's loop simply ends, others unaffected
        finally:
            conn.close()
