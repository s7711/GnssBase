import base64
import socket
import threading
import time
import unittest

from caster import STREAM_V1, STREAM_V2, Caster, parse_request, respond

MOUNT = "TESTMOUNT"


def _req(text):
    return parse_request(text.replace("\n", "\r\n").encode())


def _auth(user, password):
    return "Authorization: Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def wait_until(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class TestRespond(unittest.TestCase):
    def test_v1_stream(self):
        response, kind = respond(_req(f"GET /{MOUNT} HTTP/1.0\nUser-Agent: NTRIP test\n\n"), MOUNT)
        self.assertEqual(response, b"ICY 200 OK\r\n\r\n")
        self.assertEqual(kind, STREAM_V1)

    def test_v2_stream(self):
        response, kind = respond(_req(f"GET /{MOUNT} HTTP/1.1\nHost: x\nNtrip-Version: Ntrip/2.0\n\n"), MOUNT)
        self.assertTrue(response.startswith(b"HTTP/1.1 200 OK\r\n"))
        self.assertIn(b"Ntrip-Version: Ntrip/2.0\r\n", response)
        self.assertIn(b"Transfer-Encoding: chunked\r\n", response)
        self.assertEqual(kind, STREAM_V2)

    def test_v1_sourcetable(self):
        response, kind = respond(_req("GET / HTTP/1.0\n\n"), MOUNT)
        self.assertIsNone(kind)
        self.assertTrue(response.startswith(b"SOURCETABLE 200 OK\r\n"))
        self.assertIn(f"STR;{MOUNT};".encode(), response)
        self.assertTrue(response.endswith(b"ENDSOURCETABLE\r\n"))
        header, body = response.split(b"\r\n\r\n", 1)
        length = int(header.split(b"Content-Length: ")[1].split(b"\r\n")[0])
        self.assertEqual(length, len(body) - len(b"ENDSOURCETABLE\r\n"))

    def test_v2_sourcetable(self):
        response, kind = respond(_req("GET / HTTP/1.1\nNtrip-Version: Ntrip/2.0\n\n"), MOUNT)
        self.assertIsNone(kind)
        self.assertIn(b"Content-Type: gnss/sourcetable\r\n", response)
        header, body = response.split(b"\r\n\r\n", 1)
        self.assertEqual(int(header.split(b"Content-Length: ")[1].split(b"\r\n")[0]), len(body))
        self.assertTrue(body.endswith(b"ENDSOURCETABLE\r\n"))

    def test_sourcetable_is_anonymous_even_with_auth_configured(self):
        response, _ = respond(_req("GET / HTTP/1.0\n\n"), MOUNT, "user", "pw")
        self.assertTrue(response.startswith(b"SOURCETABLE 200 OK"))

    def test_unknown_mount_v1_gets_sourcetable_v2_gets_404(self):
        self.assertTrue(respond(_req("GET /NOPE HTTP/1.0\n\n"), MOUNT)[0].startswith(b"SOURCETABLE 200 OK"))
        self.assertTrue(respond(_req("GET /NOPE HTTP/1.1\nNtrip-Version: Ntrip/2.0\n\n"), MOUNT)[0].startswith(b"HTTP/1.1 404"))

    def test_no_auth_configured_accepts_any_credentials(self):
        _, kind = respond(_req(f"GET /{MOUNT} HTTP/1.0\n{_auth('xnav', 'whatever')}\n\n"), MOUNT)
        self.assertEqual(kind, STREAM_V1)

    def test_auth_configured(self):
        good = _req(f"GET /{MOUNT} HTTP/1.0\n{_auth('rover', 'secret')}\n\n")
        bad = _req(f"GET /{MOUNT} HTTP/1.0\n{_auth('rover', 'wrong')}\n\n")
        none = _req(f"GET /{MOUNT} HTTP/1.0\n\n")
        self.assertEqual(respond(good, MOUNT, "rover", "secret")[1], STREAM_V1)
        for req in (bad, none):
            response, kind = respond(req, MOUNT, "rover", "secret")
            self.assertIsNone(kind)
            self.assertTrue(response.startswith(b"HTTP/1.0 401"))

    def test_garbage_and_other_methods(self):
        self.assertTrue(respond(parse_request(b"\x00\x01"), MOUNT)[0].startswith(b"HTTP/1.0 400"))
        self.assertTrue(respond(_req(f"SOURCE pw /{MOUNT}\n\n"), MOUNT)[0].startswith(b"HTTP/1.0 405"))

    def test_bare_lf_request(self):
        req = parse_request(f"GET /{MOUNT} HTTP/1.0\nUser-Agent: x\n\n".encode())
        self.assertEqual(req.mountpoint, MOUNT)
        self.assertEqual(req.headers["user-agent"], "x")


class TestCasterSockets(unittest.TestCase):
    def setUp(self):
        self.caster = Caster(host="127.0.0.1", port=0, mountpoint=MOUNT, queue_len=5)
        self.caster.start()

    def tearDown(self):
        self.caster.stop()

    def connect(self, request: str):
        sock = socket.create_connection(("127.0.0.1", self.caster.port), timeout=3)
        sock.sendall(request.replace("\n", "\r\n").encode())
        return sock

    @staticmethod
    def recv_exact(sock, n):
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                break
            buf += chunk
        return buf

    def test_v1_client_gets_stream_and_its_gga_is_ignored(self):
        sock = self.connect(f"GET /{MOUNT} HTTP/1.0\n\n")
        self.assertEqual(self.recv_exact(sock, 14), b"ICY 200 OK\r\n\r\n")
        self.assertTrue(wait_until(lambda: len(self.caster.clients()) == 1))
        for _ in range(50):
            sock.sendall(b"$GPGGA,ignored*00\r\n")
        self.caster.broadcast(b"\xd3hello")
        self.caster.broadcast(b"\xd3world")
        self.assertEqual(self.recv_exact(sock, 12), b"\xd3hello\xd3world")
        sock.close()
        self.assertTrue(wait_until(lambda: not self.caster.clients()))

    def test_v2_client_gets_chunks(self):
        sock = self.connect(f"GET /{MOUNT} HTTP/1.1\nHost: x\nNtrip-Version: Ntrip/2.0\n\n")
        header = b""
        while not header.endswith(b"\r\n\r\n"):
            header += sock.recv(1)
        self.assertTrue(header.startswith(b"HTTP/1.1 200 OK"))
        self.assertTrue(wait_until(lambda: len(self.caster.clients()) == 1))
        self.caster.broadcast(b"\xd3" * 20)
        self.assertEqual(self.recv_exact(sock, 26), b"14\r\n" + b"\xd3" * 20 + b"\r\n")
        sock.close()

    def test_sourcetable_request_closes(self):
        sock = self.connect("GET / HTTP/1.0\n\n")
        data = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        self.assertTrue(data.startswith(b"SOURCETABLE 200 OK"))
        self.assertEqual(self.caster.clients(), [])

    def test_stalled_client_is_dropped_without_affecting_others(self):
        stalled = self.connect(f"GET /{MOUNT} HTTP/1.0\n\n")
        stalled.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        healthy = self.connect(f"GET /{MOUNT} HTTP/1.0\n\n")
        self.assertTrue(wait_until(lambda: len(self.caster.clients()) == 2))

        received = bytearray()

        def read_healthy():
            while True:
                try:
                    chunk = healthy.recv(65536)
                except OSError:
                    return
                if not chunk:
                    return
                received.extend(chunk)

        threading.Thread(target=read_healthy, daemon=True).start()
        block = b"\xd3" * 65536
        start = time.time()
        sent = 0
        while len(self.caster.clients()) == 2 and time.time() - start < 10:
            self.caster.broadcast(block)  # must never block, however full the stalled client is
            sent += len(block)
            time.sleep(0.001)
        self.assertLess(time.time() - start, 10, "stalled client was never dropped")
        self.assertEqual([c.addr[1] for c in self.caster.clients()], [healthy.getsockname()[1]])
        self.assertEqual(self.caster.dropped_slow, 1)

        self.caster.broadcast(b"END")
        self.assertTrue(wait_until(lambda: received.endswith(b"END")))
        self.assertEqual(len(received), len(b"ICY 200 OK\r\n\r\n") + sent + 3)
        stalled.close()
        healthy.close()

    def test_client_limit(self):
        self.caster.max_clients = 1
        first = self.connect(f"GET /{MOUNT} HTTP/1.0\n\n")
        self.assertTrue(wait_until(lambda: len(self.caster.clients()) == 1))
        second = self.connect(f"GET /{MOUNT} HTTP/1.0\n\n")
        self.assertTrue(self.recv_exact(second, 12).startswith(b"HTTP/1.0 503"))
        first.close()
        second.close()
