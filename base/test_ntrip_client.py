import base64
import json
import tempfile
import time
import unittest
from pathlib import Path

from caster import Caster
from ntrip_client import (
    CONNECTED,
    WAITING,
    Backoff,
    NtripClient,
    NtripError,
    build_request,
    parse_response_header,
)

MOUNT = "TESTMOUNT"


def wait_until(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class TestBackoff(unittest.TestCase):
    def test_doubles_to_the_cap(self):
        backoff = Backoff(30, 960)
        self.assertEqual([backoff.next_delay() for _ in range(8)], [30, 60, 120, 240, 480, 960, 960, 960])

    def test_reset_goes_back_to_initial_not_zero(self):
        backoff = Backoff(30, 960)
        backoff.next_delay()
        backoff.next_delay()
        backoff.reset()
        self.assertEqual(backoff.next_delay(), 30)


class TestProtocol(unittest.TestCase):
    def test_request(self):
        req = build_request("OXTS1", "user", "pw").decode()
        self.assertTrue(req.startswith("GET /OXTS1 HTTP/1.0\r\n"))
        self.assertIn("Authorization: Basic " + base64.b64encode(b"user:pw").decode(), req)
        self.assertTrue(req.endswith("\r\n\r\n"))
        self.assertNotIn("Authorization", build_request("OXTS1", "", "").decode())

    def test_accepts_icy_and_http_200(self):
        parse_response_header(b"ICY 200 OK\r\n\r\n")
        parse_response_header(b"HTTP/1.0 200 OK\r\nContent-Type: gnss/data\r\n\r\n")

    def test_rejects(self):
        for header in (
            b"SOURCETABLE 200 OK\r\n\r\n",
            b"HTTP/1.0 401 Unauthorized\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n",
            b"HTTP/1.1 503 Busy\r\n\r\n",
        ):
            with self.assertRaises(NtripError, msg=header):
                parse_response_header(header)


class TestClientAgainstCaster(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.persist = Path(self.tmp.name) / "ntrip.json"
        self.caster = Caster(host="127.0.0.1", port=0, mountpoint=MOUNT, user="u", password="p")
        self.caster.start()
        self.received = bytearray()
        self.client = None

    def tearDown(self):
        if self.client:
            self.client.stop()
        self.caster.stop()
        self.tmp.cleanup()

    def make_client(self, **overrides):
        cfg = {"host": "127.0.0.1", "port": self.caster.port, "mountpoint": MOUNT, "user": "u", "password": "p",
               "backoff_initial_s": 0.2, "backoff_max_s": 1.0}
        cfg.update(overrides)
        self.client = NtripClient(cfg, self.received.extend, self.persist)
        return self.client

    def test_streams_rtcm_and_forwards_gga(self):
        client = self.make_client()
        client.start()
        self.assertTrue(wait_until(lambda: client.state == CONNECTED and self.caster.clients()))
        self.caster.broadcast(b"\xd3abc")
        self.assertTrue(wait_until(lambda: bytes(self.received) == b"\xd3abc"))
        self.assertEqual(client.snapshot()["bytes_received"], 4)
        client.send_gga(b"$GNGGA,test*00\r\n")  # the caster discards it; this just must not raise or block
        time.sleep(1.2)
        self.assertEqual(client.state, CONNECTED)

    def test_dropped_connection_backs_off_and_doubles(self):
        client = self.make_client()
        client.start()
        for expected_failures in (1, 2):
            self.assertTrue(wait_until(lambda: self.caster.clients()))
            self.caster._drop(self.caster.clients()[0], "test drop")
            self.assertTrue(wait_until(lambda: client.backoff.failures == expected_failures))
            self.assertEqual(client.state, WAITING)
        self.assertTrue(wait_until(lambda: json.loads(self.persist.read_text())["failures"] == 2))
        saved = json.loads(self.persist.read_text())
        # Second wait is 0.4 s (0.2 doubled)
        self.assertAlmostEqual(saved["next_attempt_at"] - time.time(), 0.4, delta=0.15)

    def test_bad_login_backs_off(self):
        client = self.make_client(password="wrong")
        client.start()
        self.assertTrue(wait_until(lambda: client.backoff.failures >= 1))
        self.assertIn("401", client.last_error)
        self.assertEqual(self.caster.clients(), [])

    def test_persisted_wait_survives_restart(self):
        self.persist.write_text(json.dumps({"next_attempt_at": time.time() + 60, "failures": 3}))
        client = self.make_client(backoff_max_s=120)
        client.start()
        time.sleep(0.5)
        self.assertEqual(client.state, WAITING)
        self.assertEqual(self.caster.requests_served, 0)
        self.assertGreater(client.snapshot()["retry_in_s"], 55)
        self.assertEqual(client.backoff.failures, 3)

    def test_persisted_wait_is_capped_at_max(self):
        # A clock that boots behind (no RTC) must not stretch the wait past backoff_max_s.
        self.persist.write_text(json.dumps({"next_attempt_at": time.time() + 3600, "failures": 3}))
        client = self.make_client()
        client.start()
        self.assertTrue(wait_until(lambda: client.state == CONNECTED, timeout=5))

    def test_wait_follows_a_clock_step(self):
        offset = [-3600.0]  # boots an hour behind, then NTP steps it forward
        self.persist.write_text(json.dumps({"next_attempt_at": time.time() + 10, "failures": 0}))
        cfg = {"host": "127.0.0.1", "port": self.caster.port, "mountpoint": MOUNT, "user": "u", "password": "p",
               "backoff_max_s": 7200}
        self.client = NtripClient(cfg, self.received.extend, self.persist, clock=lambda: time.time() + offset[0])
        self.client.start()
        time.sleep(0.3)
        self.assertEqual(self.client.state, WAITING)
        offset[0] = 20.0
        self.assertTrue(wait_until(lambda: self.client.state == CONNECTED, timeout=8))
        # A later step mustn't show up in "connected for" either.
        offset[0] = 3600.0
        self.assertLess(self.client.snapshot()["connected_for_s"], 5)

    def test_every_attempt_claims_the_minimum_gap_first(self):
        client = self.make_client(backoff_initial_s=30)
        client.start()
        self.assertTrue(wait_until(lambda: client.state == CONNECTED))
        saved = json.loads(self.persist.read_text())
        self.assertGreater(saved["next_attempt_at"] - time.time(), 25)


class FakeIcyCaster:
    """Answers one connection with `response`, then holds it open."""

    def __init__(self, response: bytes):
        import socket
        import threading

        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        self.port = self.server.getsockname()[1]
        self.conns = []

        def serve():
            conn, _ = self.server.accept()
            self.conns.append(conn)
            conn.recv(4096)
            conn.sendall(response)

        threading.Thread(target=serve, daemon=True).start()

    def close(self):
        for c in self.conns:
            c.close()
        self.server.close()


class TestIcyWithoutBlankLine(unittest.TestCase):
    def check(self, response, expected_stream):
        tmp = tempfile.TemporaryDirectory()
        caster = FakeIcyCaster(response)
        received = bytearray()
        client = NtripClient({"host": "127.0.0.1", "port": caster.port, "mountpoint": "M"}, received.extend,
                             Path(tmp.name) / "n.json")
        try:
            client.start()
            self.assertTrue(wait_until(lambda: bytes(received) == expected_stream), bytes(received))
            self.assertEqual(client.state, CONNECTED)
        finally:
            client.stop()
            caster.close()
            tmp.cleanup()

    def test_stream_straight_after_icy_line(self):
        # Binary data that happens to contain CRLFCRLF must not be taken as the header end.
        self.check(b"ICY 200 OK\r\n\xd3\x00\x13\r\n\r\nrest", b"\xd3\x00\x13\r\n\r\nrest")

    def test_icy_with_blank_line(self):
        self.check(b"ICY 200 OK\r\n\r\n\xd3data", b"\xd3data")
