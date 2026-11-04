import os
import pickle
import socket
import struct
import tempfile
import time
import unittest

from feed_client import FeedClient


def wait_until(predicate, timeout=1.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class FakeFeedServer:
    """A minimal one-connection-at-a-time Unix-socket feed server, using
    the real wire framing, for testing FeedClient without needing a real
    drive_feed/nav_feed process running."""

    def __init__(self, socket_path):
        self.socket_path = socket_path
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(socket_path)
        self._server.listen(1)
        self._conn = None

    def accept(self):
        self._conn, _ = self._server.accept()

    def send(self, payload):
        data = pickle.dumps(payload)
        self._conn.sendall(struct.pack(">I", len(data)) + data)

    def close(self):
        if self._conn:
            self._conn.close()
        self._server.close()
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass


class TestFeedClient(unittest.TestCase):
    def setUp(self):
        self.socket_path = os.path.join(tempfile.mkdtemp(), "test-feed.sock")

    def test_default_before_any_connection(self):
        client = FeedClient(self.socket_path, default={"nav": {}, "status": {}})
        self.assertEqual(client.latest(), {"nav": {}, "status": {}})

    def test_receives_a_published_payload(self):
        server = FakeFeedServer(self.socket_path)
        client = FeedClient(self.socket_path, default={})
        client.start()

        server.accept()
        server.send({"n": 1})
        self.assertTrue(wait_until(lambda: client.latest() == {"n": 1}))
        server.close()

    def test_reconnects_after_server_restarts(self):
        server = FakeFeedServer(self.socket_path)
        client = FeedClient(self.socket_path, default={})
        client.start()

        server.accept()
        server.send({"n": 1})
        self.assertTrue(wait_until(lambda: client.latest() == {"n": 1}))
        server.close()

        server2 = FakeFeedServer(self.socket_path)
        server2.accept()
        server2.send({"n": 2})
        self.assertTrue(wait_until(lambda: client.latest() == {"n": 2}, timeout=3.0))
        server2.close()


if __name__ == "__main__":
    unittest.main()
