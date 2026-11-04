import unittest

from pyubx2 import GET, UBXMessage

from framing import NMEA, RTCM, UBX, StreamFramer, crc24q, nmea_checksum_ok, rtcm_frame, rtcm_type

GGA = b"$GNGGA,120000.00,5130.00000,N,00100.00000,W,1,12,0.80,100.0,M,47.0,M,,*6F\r\n"


def _gga():
    # Recompute the checksum so the fixture can't drift from a typo.
    body = GGA[1 : GGA.index(b"*")]
    calc = 0
    for b in body:
        calc ^= b
    return b"$" + body + b"*%02X\r\n" % calc


def _rtcm(msg_type, n_extra=20):
    payload = bytes(((msg_type >> 4) & 0xFF, (msg_type & 0x0F) << 4)) + bytes(range(n_extra))
    return rtcm_frame(payload)


def _ubx():
    return UBXMessage("NAV", "NAV-PVT", GET, fixType=3, numSV=12).serialize()


class TestCrc(unittest.TestCase):
    def test_crc24q_check_value(self):
        # Standard CRC-24Q check value for the ASCII "123456789".
        self.assertEqual(crc24q(b"123456789"), 0xCDE703)

    def test_nmea_checksum(self):
        self.assertTrue(nmea_checksum_ok(_gga()[:-2]))
        self.assertFalse(nmea_checksum_ok(_gga()[:-4] + b"00"))


class TestFramer(unittest.TestCase):
    def setUp(self):
        self.stream = _ubx() + _rtcm(1074) + _gga() + _rtcm(1005, 16) + _ubx()

    def kinds(self, frames):
        return [k for k, _ in frames]

    def test_whole_stream_in_one_read(self):
        frames = StreamFramer().feed(self.stream)
        self.assertEqual(self.kinds(frames), [UBX, RTCM, NMEA, RTCM, UBX])
        self.assertEqual(b"".join(f for _, f in frames), self.stream)
        self.assertEqual(rtcm_type(frames[1][1]), 1074)
        self.assertEqual(rtcm_type(frames[3][1]), 1005)

    def test_byte_at_a_time(self):
        framer = StreamFramer()
        frames = []
        for i in range(len(self.stream)):
            frames += framer.feed(self.stream[i : i + 1])
        self.assertEqual(b"".join(f for _, f in frames), self.stream)
        self.assertEqual(framer.discarded, 0)

    def test_every_split_point(self):
        for cut in range(1, len(self.stream)):
            framer = StreamFramer()
            frames = framer.feed(self.stream[:cut]) + framer.feed(self.stream[cut:])
            self.assertEqual(self.kinds(frames), [UBX, RTCM, NMEA, RTCM, UBX], cut)

    def test_garbage_between_frames_is_skipped(self):
        framer = StreamFramer()
        frames = framer.feed(b"\x00\xb5\x01$junk\xd3\xff" + _rtcm(1094) + b"\xb5" + _ubx())
        self.assertEqual(self.kinds(frames), [RTCM, UBX])
        self.assertGreater(framer.discarded, 0)

    def test_corrupt_rtcm_is_dropped_and_next_frame_found(self):
        bad = bytearray(_rtcm(1084))
        bad[10] ^= 0xFF
        frames = StreamFramer().feed(bytes(bad) + _rtcm(1124))
        self.assertEqual([rtcm_type(f) for _, f in frames], [1124])

    def test_corrupt_ubx_is_dropped(self):
        bad = bytearray(_ubx())
        bad[20] ^= 0x01
        frames = StreamFramer().feed(bytes(bad) + _ubx())
        self.assertEqual(self.kinds(frames), [UBX])

    def test_incomplete_frame_waits(self):
        framer = StreamFramer()
        frame = _rtcm(1230)
        self.assertEqual(framer.feed(frame[:-1]), [])
        self.assertEqual(framer.feed(frame[-1:]), [(RTCM, frame)])
