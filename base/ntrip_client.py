"""NTRIP v1 client for Locate mode: pulls RTCM from the configured caster
(OXTS1 to start with) and hands it to the F9P, and forwards the F9P's
own GGA to the caster.

Gentle with the caster, since RTK2go banned GR6's IP for a day over
reconnect loops:
- Every reconnect waits at least backoff_initial_s (30 s). Each failed
  or short-lived connection doubles the wait, up to backoff_max_s. Only
  a connection that delivered data for stable_s resets it.
- The next-allowed-attempt time is saved to disk before each attempt,
  so restarting the service (or systemd restarting a crashing one)
  can't skip the wait.
"""

import base64
import json
import logging
import socket
import threading
import time
from pathlib import Path

USER_AGENT = "NTRIP GnssBase/1.0"
CONNECT_TIMEOUT_S = 10
NO_DATA_TIMEOUT_S = 30
STABLE_S = 120
WAIT_POLL_S = 5

DISABLED = "disabled"
WAITING = "waiting"
CONNECTING = "connecting"
CONNECTED = "connected"


class Backoff:
    def __init__(self, initial_s=30.0, max_s=960.0):
        self.initial_s = initial_s
        self.max_s = max_s
        self.failures = 0

    def next_delay(self) -> float:
        """Delay before the next attempt, after a failure or drop."""
        delay = min(self.initial_s * 2 ** self.failures, self.max_s)
        self.failures += 1
        return delay

    def reset(self):
        self.failures = 0


class NtripError(Exception):
    pass


def build_request(mountpoint: str, user: str, password: str) -> bytes:
    lines = [f"GET /{mountpoint} HTTP/1.0", f"User-Agent: {USER_AGENT}", "Accept: */*"]
    if user:
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        lines.append(f"Authorization: Basic {token}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


def parse_response_header(header: bytes):
    """Check a caster's response header (up to and including the blank
    line). Raises NtripError unless it's a plain stream we can use."""
    text = header.decode("latin-1")
    first = text.split("\r\n", 1)[0].strip()
    if first.startswith("ICY 200"):
        return
    if first.startswith("HTTP/") and " 200" in first:
        if "chunked" in text.lower():
            raise NtripError(f"caster answered with a chunked (NTRIP v2) stream, not supported: {first}")
        return
    if first.startswith("SOURCETABLE"):
        raise NtripError("caster sent its source table - mountpoint not found?")
    if " 401" in first:
        raise NtripError(f"caster rejected the login: {first}")
    raise NtripError(f"unexpected caster response: {first[:80]!r}")


class NtripClient:
    def __init__(self, cfg: dict, on_rtcm, persist_path: Path, clock=time.time, monotonic=time.monotonic):
        self.host = cfg["host"]
        self.port = int(cfg.get("port", 2101))
        self.mountpoint = cfg["mountpoint"]
        self.user = cfg.get("user") or ""
        self.password = cfg.get("password") or ""
        self.backoff = Backoff(float(cfg.get("backoff_initial_s", 30)), float(cfg.get("backoff_max_s", 960)))
        self.stable_s = float(cfg.get("stable_s", STABLE_S))
        self._on_rtcm = on_rtcm
        self._persist_path = Path(persist_path)
        self._clock = clock  # wall clock: next_attempt_at is persisted across reboots
        self._mono = monotonic  # everything else - immune to the boot-time NTP step (no RTC)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._gga = None
        self.state = DISABLED
        self.last_error = None
        self.next_attempt_at = 0.0
        self.connected_at = None
        self.bytes_received = 0
        self.last_data_at = None
        self.connects = 0
        self.gga_sent = 0
        self._load_persisted()

    # -- control --

    def start(self):
        self._stop.clear()
        if self._thread and self._thread.is_alive():
            return  # a stop() whose join timed out mid-connect: that thread carries on
        self._thread = threading.Thread(target=self._run, daemon=True, name="ntrip-client")
        self._thread.start()

    def stop(self):
        if self._thread and self._thread.is_alive():
            logging.info("[ntrip] Stopping (leaving Locate mode)")
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.state = DISABLED

    def send_gga(self, line: bytes):
        """Called from the F9P reader. Never blocks: the client's own
        thread sends it."""
        with self._lock:
            self._gga = line

    def snapshot(self) -> dict:
        now, mono = self._clock(), self._mono()
        return {
            "state": self.state,
            "caster": f"{self.host}:{self.port}/{self.mountpoint}",
            "last_error": self.last_error,
            "retry_in_s": max(0.0, self.next_attempt_at - now) if self.state == WAITING else None,
            "connected_for_s": mono - self.connected_at if self.connected_at and self.state == CONNECTED else None,
            "bytes_received": self.bytes_received,
            "data_age_s": mono - self.last_data_at if self.last_data_at else None,
            "connects": self.connects,
            "gga_sent": self.gga_sent,
            "failures": self.backoff.failures,
        }

    # -- persistence --

    def _load_persisted(self):
        try:
            data = json.loads(self._persist_path.read_text())
            self.next_attempt_at = float(data.get("next_attempt_at", 0))
            self.backoff.failures = int(data.get("failures", 0))
        except (FileNotFoundError, ValueError, TypeError):
            pass
        # The Zero has no RTC: after a power cut the clock starts behind until
        # NTP syncs, which would stretch the saved wait. Never wait past the cap.
        self.next_attempt_at = min(self.next_attempt_at, self._clock() + self.backoff.max_s)

    def _persist(self):
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._persist_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"next_attempt_at": self.next_attempt_at, "failures": self.backoff.failures}))
            tmp.replace(self._persist_path)
        except OSError as e:
            logging.warning("[ntrip] Couldn't save backoff state: %s", e)

    # -- the connection loop --

    def _run(self):
        while not self._stop.is_set():
            # Re-read the clock every few seconds so an NTP step is noticed.
            while (wait := self.next_attempt_at - self._clock()) > 0:
                self.state = WAITING
                if self._stop.wait(min(wait, WAIT_POLL_S)):
                    break
            if self._stop.is_set():
                break
            # Claim the minimum gap before connecting, so a crash mid-connection still waits.
            self.next_attempt_at = self._clock() + self.backoff.initial_s
            self._persist()
            stable = False
            try:
                stable = self._session()
            except (OSError, NtripError) as e:
                self.last_error = str(e) or type(e).__name__
                logging.warning("[ntrip] %s:%d/%s: %s", self.host, self.port, self.mountpoint, self.last_error)
            finally:
                self.connected_at = None
                self.state = WAITING
            if self._stop.is_set():
                break
            if stable:
                self.backoff.reset()
            delay = self.backoff.next_delay()
            self.next_attempt_at = self._clock() + delay
            self._persist()
            logging.info("[ntrip] Next attempt in %.0f s", delay)
        self.state = DISABLED

    def _session(self) -> bool:
        """One connection. Returns True if it delivered data for at least
        stable_s before ending (which resets the backoff)."""
        self.state = CONNECTING
        logging.info("[ntrip] Connecting to %s:%d/%s", self.host, self.port, self.mountpoint)
        with socket.create_connection((self.host, self.port), timeout=CONNECT_TIMEOUT_S) as sock:
            sock.sendall(build_request(self.mountpoint, self.user, self.password))
            header, leftover = self._read_header(sock)
            parse_response_header(header)
            self.state = CONNECTED
            self.connects += 1
            self.last_error = None
            self.connected_at = self._mono()
            logging.info("[ntrip] Connected: %s", header.split(b"\r\n", 1)[0].decode("latin-1"))
            if leftover:
                self._got_data(leftover)
            sock.settimeout(1.0)
            last_data = self._mono()
            while not self._stop.is_set():
                self._send_pending_gga(sock)
                try:
                    data = sock.recv(4096)
                except socket.timeout:
                    if self._mono() - last_data > NO_DATA_TIMEOUT_S:
                        raise NtripError(f"no data for {NO_DATA_TIMEOUT_S} s")
                    continue
                if not data:
                    raise NtripError("caster closed the connection")
                last_data = self._mono()
                self._got_data(data)
            return self._mono() - self.connected_at >= self.stable_s

    @staticmethod
    def _read_header(sock):
        """Returns (header, any stream bytes after it). A v1 caster may send
        "ICY 200 OK\r\n" and go straight into the stream without the blank
        line (ntrip.oxts.com does), so for ICY the header is just that line."""
        buf = b""
        while True:
            if buf.startswith(b"ICY") and b"\r\n" in buf:
                head, _, rest = buf.partition(b"\r\n")
                return head + b"\r\n", rest[2:] if rest.startswith(b"\r\n") else rest
            if not buf.startswith(b"ICY") and b"\r\n\r\n" in buf:
                head, _, rest = buf.partition(b"\r\n\r\n")
                return head + b"\r\n\r\n", rest
            chunk = sock.recv(4096)
            if not chunk:
                raise NtripError(f"caster closed the connection during the handshake: {buf[:80]!r}")
            buf += chunk
            if len(buf) > 16384:
                raise NtripError(f"no end to the caster's response header: {buf[:80]!r}")

    def _got_data(self, data: bytes):
        self.bytes_received += len(data)
        self.last_data_at = self._mono()
        self._on_rtcm(data)

    def _send_pending_gga(self, sock):
        with self._lock:
            line, self._gga = self._gga, None
        if line:
            sock.sendall(line)
            self.gga_sent += 1
