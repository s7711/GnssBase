import struct
import unittest

from pyubx2 import GET, UBXMessage, UBXReader

from status import ReceiverStatus, decode_nav_sat, hpposecef_values


def parsed(*args, **kwargs):
    return UBXReader.parse(UBXMessage(*args, **kwargs).serialize())


def mon_rf():
    def block(block_id, ant_status, agc):
        return struct.pack("<BBBBIIHHBbBbB3s", block_id, 1, ant_status, 1, 0, 0, 90, agc, 12, 0, 0, 0, 0, b"\0\0\0")
    payload = struct.pack("<BBBB", 0, 2, 0, 0) + block(0, 2, 4096) + block(1, 4, 8191)
    return parsed("MON", "MON-RF", GET, payload=payload)


class TestReceiverStatus(unittest.TestCase):
    def setUp(self):
        self.status = ReceiverStatus(clock=lambda: 100.0)

    def test_pvt(self):
        self.status.update(parsed("NAV", "NAV-PVT", GET, fixType=3, carrSoln=2, gnssFixOk=1, numSV=21,
                                  hAcc=14, vAcc=20, lastCorrectionAge=2))
        pvt = self.status.snapshot()["pvt"]
        self.assertEqual((pvt["fix_type_name"], pvt["carr_soln_name"], pvt["num_sv"]), ("3D", "Fixed", 21))
        self.assertEqual((pvt["h_acc_m"], pvt["correction_age"]), (0.014, "1-2 s"))

    def test_hpposecef_units(self):
        msg = parsed("NAV", "NAV-HPPOSECEF", GET, iTOW=5000, ecefX=400000012, _HPecefX=0.35,
                     ecefY=-1234567, _HPecefY=-0.89, ecefZ=490000000, pAcc=12.3)
        itow, ecef, pacc = hpposecef_values(msg)
        self.assertEqual(itow, 5000)
        self.assertAlmostEqual(ecef[0], 4000000.1235, places=6)
        self.assertAlmostEqual(ecef[1], -12345.6789, places=6)
        self.assertAlmostEqual(pacc, 0.0123, places=6)

    def test_sat_summary(self):
        self.status.update(parsed("NAV", "NAV-SAT", GET, numSvs=3,
                                  gnssId_01=0, svId_01=5, cno_01=40, svUsed_01=1,
                                  gnssId_02=6, svId_02=3, cno_02=20, svUsed_02=0,
                                  gnssId_03=0, svId_03=9, cno_03=0, svUsed_03=0))
        sats = self.status.snapshot()["sats"]
        self.assertEqual((sats["tracked"], sats["used"]), (2, 1))
        self.assertEqual(sats["summary"][0], {"gnss": "GPS", "tracked": 1, "used": 1})
        self.assertEqual([(s["gnss"], s["sv"]) for s in sats["sats"]], [("GPS", 5), ("GPS", 9), ("GLONASS", 3)])

    def test_mon_rf(self):
        self.status.update(mon_rf())
        rf = self.status.snapshot()["rf"]
        self.assertEqual([(b["block"], b["ant_status"]) for b in rf], [("L1", "OK"), ("L2/L5", "Open")])
        self.assertAlmostEqual(rf[1]["agc_percent"], 100.0)

    def test_mon_ver(self):
        payload = (b"EXT CORE 1.00 (a)".ljust(30, b"\0") + b"00190000".ljust(10, b"\0")
                   + b"FWVER=HPG 1.51".ljust(30, b"\0") + b"PROTVER=27.50".ljust(30, b"\0"))
        self.status.update(parsed("MON", "MON-VER", GET, payload=payload))
        version = self.status.snapshot()["version"]
        self.assertEqual((version["fw"], version["protver"], version["hw"]), ("HPG 1.51", "27.50", "00190000"))

    def test_svin(self):
        self.status.update(parsed("NAV", "NAV-SVIN", GET, dur=60, meanX=400000012, meanXHP=35,
                                  meanAcc=12345, obs=60, valid=1, active=0))
        svin = self.status.snapshot()["svin"]
        self.assertAlmostEqual(svin["mean_ecef_m"][0], 4000000.1235, places=6)
        self.assertAlmostEqual(svin["mean_acc_m"], 1.2345)
        self.assertTrue(svin["valid"])

    def test_raw_nav_sat_decode_matches_pyubx2(self):
        kwargs = {"numSvs": 40}
        for i in range(1, 41):
            kwargs.update({f"gnssId_{i:02d}": i % 7, f"svId_{i:02d}": i, f"cno_{i:02d}": i % 50,
                           f"elev_{i:02d}": i - 20, f"azim_{i:02d}": i * 9, f"svUsed_{i:02d}": i % 2,
                           f"health_{i:02d}": 1, f"ephAvail_{i:02d}": 1})
        frame = UBXMessage("NAV", "NAV-SAT", GET, **kwargs).serialize()
        self.status.update(UBXReader.parse(frame))
        via_pyubx2 = self.status.snapshot()["sats"]
        self.assertEqual(decode_nav_sat(frame), via_pyubx2)
        self.assertEqual(via_pyubx2["used"], 20)
