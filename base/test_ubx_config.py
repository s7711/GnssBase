import threading
import unittest

from pyubx2 import SET, UBXReader

from ubx_config import (
    BASE,
    LOCATE,
    SURVEY,
    AckTracker,
    apply_config,
    mode_config,
    split_ecef,
    valset,
)

CFG = {
    "rtcm": {"rate_1005": 10, "rate_1230": 10},
    "survey_in": {"min_duration_s": 120, "accuracy_limit_m": 1.5},
    "ntrip_client": {"gga_interval_s": 10},
}
# Made-up position (roughly mid-Atlantic), never a real site.
POSITION = {"ecef_m": [4000000.1235, -12345.6789, 4900000.00004], "sd_3d_m": 0.004}


class TestSplitEcef(unittest.TestCase):
    def test_positive(self):
        self.assertEqual(split_ecef(4000000.1235), (400000012, 35))  

    def test_negative_keeps_sign(self):
        self.assertEqual(split_ecef(-12345.6789), (-1234567, -89))

    def test_reassembles(self):
        for metres in (4000000.1235, -12345.6789, 4900000.00004, -0.00049, 6378137.0):
            cm, hp = split_ecef(metres)
            self.assertLessEqual(abs(hp), 99)
            self.assertAlmostEqual(cm / 100 + hp / 10000, round(metres, 4), places=6)


class TestModeConfig(unittest.TestCase):
    def test_locate_is_rover_with_gga_and_no_rtcm_out(self):
        items = dict(mode_config(LOCATE, CFG))
        self.assertEqual(items["CFG_TMODE_MODE"], 0)
        self.assertEqual(items["CFG_MSGOUT_NMEA_ID_GGA_USB"], 10)
        self.assertEqual(items["CFG_MSGOUT_RTCM_3X_TYPE1074_USB"], 0)
        self.assertEqual(items["CFG_MSGOUT_RTCM_3X_TYPE1005_USB"], 0)
        self.assertEqual(items["CFG_MSGOUT_UBX_NAV_HPPOSECEF_USB"], 1)

    def test_base_is_fixed_ecef_with_rtcm(self):
        items = dict(mode_config(BASE, CFG, POSITION))
        self.assertEqual(items["CFG_TMODE_MODE"], 2)
        self.assertEqual(items["CFG_TMODE_POS_TYPE"], 0)
        self.assertEqual((items["CFG_TMODE_ECEF_Y"], items["CFG_TMODE_ECEF_Y_HP"]), (-1234567, -89))
        self.assertEqual(items["CFG_TMODE_FIXED_POS_ACC"], 100)  # floored at 1 cm
        for t in (1074, 1084, 1094, 1124):
            self.assertEqual(items[f"CFG_MSGOUT_RTCM_3X_TYPE{t}_USB"], 1)
        self.assertEqual(items["CFG_MSGOUT_RTCM_3X_TYPE1005_USB"], 10)
        self.assertEqual(items["CFG_MSGOUT_RTCM_3X_TYPE1230_USB"], 10)
        self.assertEqual(items["CFG_MSGOUT_NMEA_ID_GGA_USB"], 0)

    def test_base_without_position_refuses(self):
        with self.assertRaises(ValueError):
            mode_config(BASE, CFG, None)

    def test_survey_in(self):
        items = dict(mode_config(SURVEY, CFG))
        self.assertEqual(items["CFG_TMODE_MODE"], 1)
        self.assertEqual(items["CFG_TMODE_SVIN_MIN_DUR"], 120)
        self.assertEqual(items["CFG_TMODE_SVIN_ACC_LIMIT"], 15000)
        self.assertEqual(items["CFG_MSGOUT_UBX_NAV_SVIN_USB"], 1)

    def test_every_mode_sets_the_same_output_keys(self):
        # Switching mode must not leave another mode's outputs running.
        def outputs(mode, pos=None):
            return {k for k, _ in mode_config(mode, CFG, pos) if not k.startswith("CFG_TMODE")}

        self.assertEqual(outputs(LOCATE), outputs(SURVEY))
        self.assertEqual(outputs(LOCATE), outputs(BASE, POSITION))

    def test_valset_round_trips_through_pyubx2(self):
        items = mode_config(BASE, CFG, POSITION)
        msg = UBXReader.parse(valset(items), msgmode=SET)
        self.assertEqual(msg.identity, "CFG-VALSET")
        self.assertEqual(msg.ram, 1)
        self.assertEqual((msg.bbr, msg.flash), (0, 0))
        self.assertEqual(msg.CFG_TMODE_ECEF_X_HP, 35)


class TestApplyConfig(unittest.TestCase):
    def test_all_acked(self):
        sent = []
        results = apply_config([("A", 1), ("B", 2), ("C", 3)], lambda c: sent.append(c) or True, chunk_size=2)
        self.assertEqual([r["result"] for r in results], ["ACK"] * 3)
        self.assertEqual(sent, [[("A", 1), ("B", 2)], [("C", 3)]])

    def test_nak_is_narrowed_to_the_rejected_key(self):
        def send(chunk):
            return not any(k == "BAD" for k, _ in chunk)

        results = apply_config([("A", 1), ("BAD", 2), ("C", 3)], send)
        self.assertEqual({r["key"]: r["result"] for r in results}, {"A": "ACK", "BAD": "NAK", "C": "ACK"})

    def test_timeout(self):
        results = apply_config([("A", 1)], lambda c: None)
        self.assertEqual(results, [{"key": "A", "value": 1, "result": "timeout"}])


class TestAckTracker(unittest.TestCase):
    def test_ack_from_another_thread(self):
        tracker = AckTracker()
        tracker.expect(0x06, 0x8A)
        threading.Timer(0.05, tracker.on_ack, args=(0x06, 0x8A, True)).start()
        self.assertIs(tracker.wait(2.0), True)

    def test_nak(self):
        tracker = AckTracker()
        tracker.expect(0x06, 0x8A)
        tracker.on_ack(0x06, 0x8A, False)
        self.assertIs(tracker.wait(0.1), False)

    def test_ack_for_another_message_is_ignored(self):
        tracker = AckTracker()
        tracker.expect(0x06, 0x8A)
        tracker.on_ack(0x06, 0x8B, True)
        self.assertIsNone(tracker.wait(0.05))

    def test_stale_ack_before_expect_is_ignored(self):
        tracker = AckTracker()
        tracker.on_ack(0x06, 0x8A, True)
        tracker.expect(0x06, 0x8A)
        self.assertIsNone(tracker.wait(0.05))
