"""A small NTRIP caster: one mountpoint, streaming the F9P's RTCM to every
connected client.

- NTRIP v1 (`GET /<mount>` -> `ICY 200 OK`, raw stream) and v2 (a
  request with `Ntrip-Version: Ntrip/2.0` -> `HTTP/1.1 200 OK`, chunked
  stream).
- `GET /` (or any unknown mountpoint, v1) returns the source table,
  anonymously - the xNAV's OxTS client asks for one on every connect.
- Optional Basic auth on the mountpoint; with no user configured, any
  credentials (or none) are accepted.
- Whatever a client sends after connecting (the xNAV sends GGA) is read
  and discarded, so it can't back up.
- Each client has its own bounded queue and writer thread. broadcast()
  never blocks: a client whose queue is full is dropped, so a slow or
  dead client can't stall the others or the F9P reader.
"""

import base64
import logging
import queue
import socket
import threading
import time
from dataclasses import dataclass, field

SERVER_NAME = "GnssBase NTRIP caster/1.0"
MAX_REQUEST_BYTES = 8192
REQUEST_TIMEOUT_S = 10
SEND_TIMEOUT_S = 10

STREAM_V1 = "v1"
STREAM_V2 = "v2"


@dataclass
class Request:
    method: str
    path: str
    http_version: str
    headers: dict = field(default_factory=dict)  # lower-cased names

    @property
    def ntrip_v2(self) -> bool:
        return "ntrip/2" in self.headers.get("ntrip-version", "").lower()

    @property
    def mountpoint(self) -> str:
        return self.path.split("?", 1)[0].lstrip("/")

    def basic_auth(self) -> tuple[str, str] | None:
        value = self.headers.get("authorization", "")
        scheme, _, token = value.partition(" ")
        if scheme.lower() != "basic":
            return None
        try:
            user, _, password = base64.b64decode(token.strip()).decode("utf-8", "replace").partition(":")
        except ValueError:
            return None
        return user, password


def parse_request(raw: bytes) -> Request | None:
    """Parse the request line and headers (everything before the blank
    line). None if it isn't a recognisable request."""
    text = raw.decode("latin-1")
    lines = text.split("\r\n") if "\r\n" in text else text.split("\n")
    parts = lines[0].split()
    if len(parts) < 2:
        return None
    method, path = parts[0].upper(), parts[1]
    http_version = parts[2] if len(parts) > 2 else "HTTP/1.0"
    headers = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    return Request(method, path, http_version, headers)


def sourcetable_body(mountpoint: str, identifier: str = "GnssBase", country: str = "GBR") -> str:
    """One STR record. Lat/lon are left at 0.00 - they're informational,
    and the source table is served to anyone who asks."""
    fmt_details = "1005(10),1074(1),1084(1),1094(1),1124(1),1230(10)"
    fields = [
        "STR", mountpoint, identifier, "RTCM 3.3", fmt_details,
        "2", "GPS+GLO+GAL+BDS", "GnssBase", country,
        "0.00", "0.00", "0", "0", "u-blox ZED-F9P", "none", "B", "N", "0", "",
    ]
    return ";".join(fields) + "\r\n"


def respond(req: Request | None, mountpoint: str, user: str = "", password: str = "") -> tuple[bytes, str | None]:
    """Decide the response to a request. Returns (response bytes, stream
    kind): STREAM_V1/STREAM_V2 to start streaming after sending the
    response, or None to send it and close."""
    if req is None:
        return b"HTTP/1.0 400 Bad Request\r\n\r\n", None
    v2 = req.ntrip_v2
    status_prefix = "HTTP/1.1" if v2 else "HTTP/1.0"
    if req.method != "GET":
        return f"{status_prefix} 405 Method Not Allowed\r\nConnection: close\r\n\r\n".encode(), None

    if req.mountpoint != mountpoint:
        if v2 and req.mountpoint:
            return f"HTTP/1.1 404 Not Found\r\nNtrip-Version: Ntrip/2.0\r\nServer: {SERVER_NAME}\r\nConnection: close\r\n\r\n".encode(), None
        return _sourcetable(mountpoint, v2), None

    if user and req.basic_auth() != (user, password):
        headers = f'WWW-Authenticate: Basic realm="/{mountpoint}"\r\nServer: {SERVER_NAME}\r\nConnection: close\r\n'
        if v2:
            headers += "Ntrip-Version: Ntrip/2.0\r\n"
        return f"{status_prefix} 401 Unauthorized\r\n{headers}\r\n".encode(), None

    if v2:
        return (
            "HTTP/1.1 200 OK\r\n"
            "Ntrip-Version: Ntrip/2.0\r\n"
            f"Server: {SERVER_NAME}\r\n"
            "Content-Type: gnss/data\r\n"
            "Cache-Control: no-store, no-cache, max-age=0\r\n"
            "Transfer-Encoding: chunked\r\n"
            "Connection: close\r\n\r\n"
        ).encode(), STREAM_V2
    return b"ICY 200 OK\r\n\r\n", STREAM_V1


def _sourcetable(mountpoint: str, v2: bool) -> bytes:
    body = sourcetable_body(mountpoint)
    if v2:
        body += "ENDSOURCETABLE\r\n"
        return (
            "HTTP/1.1 200 OK\r\n"
            "Ntrip-Version: Ntrip/2.0\r\n"
            f"Server: {SERVER_NAME}\r\n"
            "Content-Type: gnss/sourcetable\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n" + body
        ).encode()
    return (
        "SOURCETABLE 200 OK\r\n"
        f"Server: {SERVER_NAME}\r\n"
        "Content-Type: text/plain\r\n"
        f"Content-Length: {len(body)}\r\n\r\n" + body + "ENDSOURCETABLE\r\n"
    ).encode()


class _Client:
    def __init__(self, sock, addr, req, kind, queue_len):
        self.sock = sock
        self.addr = addr
        self.kind = kind
        self.user_agent = req.headers.get("user-agent", "")
        self.auth_user = (req.basic_auth() or ("", ""))[0]
        self.connected_at = time.monotonic()
        self.bytes_sent = 0
        self.queue = queue.Queue(maxsize=queue_len)
        self.closed = False

    def info(self) -> dict:
        return {
            "ip": self.addr[0],
            "port": self.addr[1],
            "ntrip": self.kind,
            "user_agent": self.user_agent,
            "user": self.auth_user,
            "connected_for_s": time.monotonic() - self.connected_at,
            "bytes_sent": self.bytes_sent,
            "queued": self.queue.qsize(),
        }


class Caster:
    def __init__(self, host="0.0.0.0", port=2101, mountpoint="GNSSBASE", user="", password="",
                 queue_len=100, max_clients=10):
        self.host = host
        self.port = port
        self.mountpoint = mountpoint
        self.user = user or ""
        self.password = password or ""
        self.queue_len = queue_len
        self.max_clients = max_clients
        self._clients: list[_Client] = []
        self._lock = threading.Lock()
        self._server = None
        self.requests_served = 0
        self.dropped_slow = 0

    def start(self):
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((self.host, self.port))
        self._server.listen(8)
        self.port = self._server.getsockname()[1]  # in case port 0 was asked for (tests)
        threading.Thread(target=self._accept_loop, daemon=True, name="caster-accept").start()
        logging.info("[caster] Listening on %s:%d, mountpoint /%s", self.host, self.port, self.mountpoint)

    def stop(self):
        if self._server:
            self._server.close()
        for client in self.clients():
            self._drop(client, "caster stopping")

    def clients(self) -> list[_Client]:
        with self._lock:
            return list(self._clients)

    def broadcast(self, data: bytes):
        """Queue `data` for every client. Never blocks."""
        for client in self.clients():
            try:
                client.queue.put_nowait(data)
            except queue.Full:
                self.dropped_slow += 1
                self._drop(client, "send queue overflow (slow or dead client)")

    def snapshot(self) -> dict:
        return {
            "port": self.port,
            "mountpoint": self.mountpoint,
            "auth": bool(self.user),
            "clients": [c.info() for c in self.clients()],
            "requests_served": self.requests_served,
            "dropped_slow": self.dropped_slow,
        }

    def _accept_loop(self):
        while True:
            try:
                sock, addr = self._server.accept()
            except OSError:
                return  # server socket closed
            threading.Thread(target=self._handle, args=(sock, addr), daemon=True, name="caster-client").start()

    def _handle(self, sock, addr):
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(REQUEST_TIMEOUT_S)
            raw = self._read_request(sock)
            req = parse_request(raw) if raw else None
            response, kind = respond(req, self.mountpoint, self.user, self.password)
            self.requests_served += 1
            if req is not None:
                # Logged in full (minus credentials) so we can see what the xNAV actually sends.
                shown = {k: v for k, v in req.headers.items() if k != "authorization"}
                logging.info("[caster] %s:%d %s %s %s headers=%s -> %s",
                             addr[0], addr[1], req.method, req.path, req.http_version, shown,
                             response.split(b"\r\n", 1)[0].decode("latin-1"))
            if kind is not None and len(self.clients()) >= self.max_clients:
                logging.warning("[caster] %s:%d refused: already %d clients", addr[0], addr[1], self.max_clients)
                response, kind = b"HTTP/1.0 503 Service Unavailable\r\n\r\n", None
            sock.sendall(response)
            if kind is None:
                return
            client = _Client(sock, addr, req, kind, self.queue_len)
            with self._lock:
                self._clients.append(client)
            logging.info("[caster] Client connected: %s:%d (%s, user-agent %r)", addr[0], addr[1], kind, client.user_agent)
            sock.settimeout(SEND_TIMEOUT_S)
            threading.Thread(target=self._writer, args=(client,), daemon=True, name="caster-writer").start()
            self._discard_incoming(client)
        except OSError as e:
            logging.info("[caster] %s:%d request failed: %s", addr[0], addr[1], e)
        finally:
            # A streaming client's socket is closed by _drop, once.
            if not any(c.sock is sock for c in self.clients()):
                try:
                    sock.close()
                except OSError:
                    pass

    @staticmethod
    def _read_request(sock) -> bytes:
        raw = b""
        while b"\r\n\r\n" not in raw and b"\n\n" not in raw:
            chunk = sock.recv(1024)
            if not chunk:
                break
            raw += chunk
            if len(raw) > MAX_REQUEST_BYTES:
                break
        return raw

    def _discard_incoming(self, client):
        """Read and throw away whatever the client sends (GGA, usually)
        until it disconnects. A recv timeout just means it's quiet."""
        while not client.closed:
            try:
                if not client.sock.recv(4096):
                    self._drop(client, "client closed the connection")
                    return
            except socket.timeout:
                continue
            except OSError as e:
                self._drop(client, f"receive error: {e}")
                return

    def _writer(self, client):
        while True:
            data = client.queue.get()
            if data is None or client.closed:
                return
            if client.kind == STREAM_V2:
                data = b"%X\r\n" % len(data) + data + b"\r\n"
            try:
                client.sock.sendall(data)
                client.bytes_sent += len(data)
            except OSError as e:
                self._drop(client, f"send failed: {e}")
                return

    def _drop(self, client, reason):
        with self._lock:
            if client.closed:
                return
            client.closed = True
            if client in self._clients:
                self._clients.remove(client)
        # Wake the writer: empty the queue so the sentinel always fits.
        try:
            while True:
                client.queue.get_nowait()
        except queue.Empty:
            pass
        client.queue.put_nowait(None)
        try:
            client.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        client.sock.close()
        logging.info("[caster] Client disconnected: %s:%d after %.0f s, %d bytes sent - %s",
                     client.addr[0], client.addr[1], time.monotonic() - client.connected_at, client.bytes_sent, reason)
