"""Splits the F9P's mixed byte stream into whole UBX, NMEA and RTCM 3
frames, however the serial reads happen to cut it.

pyubx2's UBXReader can do this too, but it reads the stream itself with
blocking read(n) calls; owning the buffer here keeps the serial loop
simple, makes the split-read cases testable, and means RTCM is passed
through to the caster as raw bytes without being decoded (pyrtcm on
every MSM message would be real work for a Zero). Every frame's
checksum/CRC is verified, so a false sync inside binary data costs one
byte of resync, not a corrupt message.
"""

import re

UBX = "UBX"
NMEA = "NMEA"
RTCM = "RTCM"

_START = re.compile(rb"[\xb5$\xd3]")
_MAX_UBX_PAYLOAD = 8192
_MAX_NMEA = 120  # NMEA 0183 allows 82; u-blox's longer proprietary sentences fit in this


def _crc24q_table():
    table = []
    for i in range(256):
        crc = i << 16
        for _ in range(8):
            crc <<= 1
            if crc & 0x1000000:
                crc ^= 0x1864CFB
        table.append(crc & 0xFFFFFF)
    return table


_CRC24Q = _crc24q_table()


def crc24q(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = ((crc << 8) & 0xFFFFFF) ^ _CRC24Q[(crc >> 16) ^ b]
    return crc


def ubx_checksum(data: bytes) -> bytes:
    """Fletcher-8 over class, id, length and payload."""
    a = b = 0
    for x in data:
        a = (a + x) & 0xFF
        b = (b + a) & 0xFF
    return bytes((a, b))


def nmea_checksum_ok(frame: bytes) -> bool:
    star = frame.rfind(b"*")
    if star < 0 or len(frame) < star + 3:
        return False
    calc = 0
    for x in frame[1:star]:
        calc ^= x
    try:
        return int(frame[star + 1 : star + 3], 16) == calc
    except ValueError:
        return False


def rtcm_type(frame: bytes) -> int:
    """Message number: the first 12 bits after the 3-byte header."""
    return (frame[3] << 4) | (frame[4] >> 4)


def rtcm_frame(payload: bytes) -> bytes:
    """Wrap a payload as an RTCM 3 frame - only needed to build test data."""
    header = bytes((0xD3, (len(payload) >> 8) & 0x03, len(payload) & 0xFF))
    crc = crc24q(header + payload)
    return header + payload + crc.to_bytes(3, "big")


class StreamFramer:
    def __init__(self):
        self._buf = bytearray()
        self.discarded = 0  # bytes skipped while hunting for sync - a rising count means line noise or a framing bug

    def feed(self, data: bytes) -> list[tuple[str, bytes]]:
        self._buf += data
        frames = []
        while True:
            frame = self._next()
            if frame is None:
                return frames
            frames.append(frame)

    def _skip(self, n):
        del self._buf[:n]
        self.discarded += n

    def _next(self):
        """Return the next whole frame, or None if more bytes are needed."""
        buf = self._buf
        while True:
            match = _START.search(buf)
            if match is None:
                self._skip(len(buf))
                return None
            if match.start():
                self._skip(match.start())
            first = buf[0]
            if first == 0xB5:
                result = self._ubx()
            elif first == 0xD3:
                result = self._rtcm()
            else:
                result = self._nmea()
            if result is _RESYNC:
                self._skip(1)
                continue
            return result

    def _ubx(self):
        buf = self._buf
        if len(buf) < 6:
            return None if len(buf) < 2 or buf[1] == 0x62 else _RESYNC
        if buf[1] != 0x62:
            return _RESYNC
        length = buf[4] | buf[5] << 8
        if length > _MAX_UBX_PAYLOAD:
            return _RESYNC
        total = 8 + length
        if len(buf) < total:
            return None
        frame = bytes(buf[:total])
        if ubx_checksum(frame[2:-2]) != frame[-2:]:
            return _RESYNC
        del buf[:total]
        return UBX, frame

    def _rtcm(self):
        buf = self._buf
        if len(buf) < 3:
            return None
        if buf[1] & 0xFC:  # six reserved bits, always zero
            return _RESYNC
        total = 3 + ((buf[1] & 0x03) << 8 | buf[2]) + 3
        if len(buf) < total:
            return None
        frame = bytes(buf[:total])
        if crc24q(frame[:-3]) != int.from_bytes(frame[-3:], "big"):
            return _RESYNC
        del buf[:total]
        return RTCM, frame

    def _nmea(self):
        buf = self._buf
        end = buf.find(b"\r\n", 0, _MAX_NMEA)
        if end < 0:
            return None if len(buf) < _MAX_NMEA else _RESYNC
        frame = bytes(buf[: end + 2])
        if not nmea_checksum_ok(frame[:-2]):
            return _RESYNC
        del buf[: end + 2]
        return NMEA, frame


_RESYNC = object()
