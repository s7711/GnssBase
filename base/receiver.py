"""Owns the F9P's serial port: one reader thread framing the stream, a
write lock shared by config messages and the NTRIP client's RTCM, and
ACK matching for configuration.

If the port goes away (the F9P reset or was unplugged), it's reopened
every few seconds and on_connect fires again, so the app reconfigures
the receiver from scratch each time - the receiver's own saved settings
are never relied on.
"""

import logging
import threading
import time

import serial

from framing import UBX, StreamFramer
from ubx_config import AckTracker

ACK_CLASS = 0x05
REOPEN_DELAY_S = 3
ACK_TIMEOUT_S = 2.0


class Receiver:
    def __init__(self, port: str, on_frame, on_connect, baud=115200):
        self.port = port
        self.baud = baud
        self._on_frame = on_frame
        self._on_connect = on_connect
        self._serial = None
        self._write_lock = threading.Lock()
        self._cfg_lock = threading.Lock()  # one CFG request in flight - see AckTracker
        self._acks = AckTracker()
        self.framer = StreamFramer()
        self.connected = False
        self.last_error = None
        self.bytes_in = 0

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="receiver").start()

    def write(self, data: bytes) -> bool:
        with self._write_lock:
            ser = self._serial
            if ser is None:
                return False
            try:
                ser.write(data)
                return True
            except (serial.SerialException, OSError) as e:
                logging.warning("[receiver] Write failed: %s", e)
                return False

    def send_cfg(self, message: bytes, cls_id: int, msg_id: int, timeout=ACK_TIMEOUT_S) -> bool | None:
        """Send a CFG message and wait for its ACK (True), NAK (False) or
        nothing (None)."""
        with self._cfg_lock:
            self._acks.expect(cls_id, msg_id)
            if not self.write(message):
                self._acks.wait(0)
                return None
            return self._acks.wait(timeout)

    def _run(self):
        while True:
            try:
                ser = serial.Serial(self.port, self.baud, timeout=0.5)
            except (serial.SerialException, OSError) as e:
                if self.last_error != str(e):
                    logging.warning("[receiver] Can't open %s: %s", self.port, e)
                self.last_error = str(e)
                time.sleep(REOPEN_DELAY_S)
                continue
            logging.info("[receiver] Opened %s", self.port)
            self._serial = ser
            self.connected = True
            self.last_error = None
            self.framer = StreamFramer()
            self._on_connect()
            try:
                self._read_loop(ser)
            except (serial.SerialException, OSError) as e:
                self.last_error = str(e)
                logging.warning("[receiver] Lost %s: %s", self.port, e)
            finally:
                with self._write_lock:
                    self._serial = None
                self.connected = False
                try:
                    ser.close()
                except (serial.SerialException, OSError):
                    pass
            time.sleep(REOPEN_DELAY_S)

    def _read_loop(self, ser):
        while True:
            data = ser.read(ser.in_waiting or 1)
            if not data:
                continue
            self.bytes_in += len(data)
            for kind, frame in self.framer.feed(data):
                if kind == UBX and frame[2] == ACK_CLASS and len(frame) == 10:
                    self._acks.on_ack(frame[6], frame[7], frame[3] == 0x01)
                    continue
                try:
                    self._on_frame(kind, frame)
                except Exception:
                    logging.exception("[receiver] Frame handler failed")
