import math
import random
import tempfile
import unittest
from pathlib import Path

import locate_log
from averager import ecef_to_enu_rotation
from locate_log import RECORD, LocateLog, Series

# Made-up site, 45N 0E on a sphere-ish Earth - not a real location.
SITE = [4517590.8794, 0.0123, 4487348.4091]
T0 = 1790000000
UP = ecef_to_enu_rotation(SITE)[2]
NORTH = ecef_to_enu_rotation(SITE)[1]


def offset(e=0.0, n=0.0, u=0.0):
    r = ecef_to_enu_rotation(SITE)
    return [SITE[i] + e * r[0][i] + n * r[1][i] + u * r[2][i] for i in range(3)]


class LogTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "data"
        self.log = self.open()

    def tearDown(self):
        self.tmp.cleanup()

    def open(self):
        log = LocateLog(self.dir)
        log.load()
        return log

    def add(self, log, t, pos, carr=2):
        log.add_epoch(t, carr, pos, 0.02, num_sv=30, h_acc_m=0.014, v_acc_m=0.021)


class TestRecording(LogTestCase):
    def test_float_epochs_before_the_first_fix_are_counted_not_averaged(self):
        self.add(self.log, T0, offset(u=5.0), carr=1)
        self.add(self.log, T0 + 1, SITE)
        self.add(self.log, T0 + 2, offset(u=0.002))
        s = self.log.summary()
        self.assertEqual((s["epochs_fixed"], s["epochs_not_fixed"]), (2, 1))
        self.assertEqual(self.log.origin, [round(v, 2) for v in SITE])
        for i in range(3):
            self.assertAlmostEqual(s["mean_ecef_m"][i], SITE[i] + 0.001 * UP[i], delta=1e-4)
        self.assertEqual(s["elapsed_s"], 2)

    def test_mean_and_sd_match_direct_computation(self):
        rng = random.Random(1)
        points = [offset(rng.gauss(0, 0.003), rng.gauss(0, 0.002), rng.gauss(0, 0.006)) for _ in range(500)]
        for k, p in enumerate(points):
            self.add(self.log, T0 + k, p)
        s = self.log.summary()
        for i in range(3):
            values = [p[i] for p in points]
            mean = sum(values) / len(values)
            sd = math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))
            self.assertAlmostEqual(s["mean_ecef_m"][i], mean, delta=1e-4)  # stored to 0.1 mm
            self.assertAlmostEqual(s["sd_ecef_m"][i], sd, delta=1e-4)
        self.assertAlmostEqual(s["sd_enu_m"][2], 0.006, delta=0.001)

    def test_survives_a_restart(self):
        for k in range(100):
            self.add(self.log, T0 + k, offset(u=0.001 * (k % 7)))
        before = self.log.summary()
        self.log._write_checkpoint()
        for k in range(100, 150):  # after the checkpoint: replayed from the file
            self.add(self.log, T0 + k, offset(u=0.01))
        before = self.log.summary()
        after = self.open().summary()
        self.assertEqual(after["epochs_fixed"], 150)
        for i in range(3):
            self.assertAlmostEqual(after["mean_ecef_m"][i], before["mean_ecef_m"][i], places=9)

    def test_checkpoint_from_another_data_set_is_ignored(self):
        for k in range(10):
            self.add(self.log, T0 + k, SITE)
        self.log._write_checkpoint()
        self.log.reset()
        self.log.checkpoint_path.write_bytes((self.dir / "locate-checkpoint.pickle.prev").read_bytes())
        self.add(self.log, T0 + 100, SITE)
        self.assertEqual(self.open().summary()["epochs_fixed"], 1)

    def test_checkpoint_from_an_older_version_is_rebuilt_from_the_epochs(self):
        for k in range(10):
            self.add(self.log, T0 + k, offset(e=0.0012 * k))
        self.log._write_checkpoint()
        import pickle
        saved = pickle.loads(self.log.checkpoint_path.read_bytes())
        saved["version"] = 1
        saved["agg"].hist = [{0: 10}, {}, {}]  # e.g. the old 5 mm buckets
        self.log.checkpoint_path.write_bytes(pickle.dumps(saved))
        hist = self.open().analysis()["hist"][0]
        self.assertEqual(sum(c for _, c in hist), 10)
        self.assertGreaterEqual(len(hist), 10)  # 1 mm buckets over a 10.8 mm spread

    def test_partial_record_from_a_power_cut_is_dropped(self):
        for k in range(5):
            self.add(self.log, T0 + k, SITE)
        with open(self.log.data_path, "ab") as f:
            f.write(b"\x01\x02\x03")
        log = self.open()
        self.assertEqual(log.summary()["epochs_fixed"], 5)
        self.assertEqual(log.data_path.stat().st_size, 5 * RECORD.size)

    def test_reset_keeps_the_previous_set(self):
        self.add(self.log, T0, SITE)
        self.log.reset()
        self.assertEqual(self.log.summary()["epochs_fixed"], 0)
        self.assertIsNone(self.log.origin)
        self.assertTrue((self.dir / "locate-epochs.bin.prev").exists())
        self.add(self.log, T0 + 5, offset(u=1.0))
        self.assertEqual(self.open().summary()["epochs_fixed"], 1)


class TestPeaks(LogTestCase):
    def test_finds_the_levels(self):
        rng = random.Random(2)
        # Right answer in three separate hours; a wrong fix, 12 cm up, for one long stretch.
        t = T0
        for hour, (n, u, count) in enumerate([(0, 0, 600), (-0.045, 0.12, 1500), (0, 0, 600), (0, 0, 600)]):
            t = T0 + hour * 3600
            for k in range(count):
                self.add(self.log, t + k, offset(rng.gauss(0, 0.002), n + rng.gauss(0, 0.002), u + rng.gauss(0, 0.003)))
        peaks = self.log.peaks()
        top = peaks[0]
        self.assertGreater(top["epochs"], 1700)
        self.assertLess(max(abs(x) for x in top["mean_enu_m"]), 0.002 + 0.006)  # origin rounding is up to 5 mm per axis
        wrong = peaks[1]
        self.assertGreater(wrong["epochs"], 1400)
        self.assertLessEqual(sum(p["epochs"] for p in peaks), 3300)  # peaks don't overlap: no epoch counted twice
        self.assertAlmostEqual(wrong["mean_enu_m"][2] - top["mean_enu_m"][2], 0.12, delta=0.002)

        pos = self.log.peak_position(top["id"])
        for i in range(3):
            self.assertAlmostEqual(pos["mean_ecef_m"][i], SITE[i], delta=0.002)

    def test_overall_mean_is_a_candidate(self):
        with self.assertRaises(ValueError):
            self.log.peak_position("all")  # nothing yet
        for k, u in enumerate([0.0, 0.01, 0.02, 0.03]):
            self.add(self.log, T0 + k * 3600, offset(u=u))
        overall = self.log.analysis()["overall"]
        self.assertEqual((overall["id"], overall["epochs"]), ("all", 4))
        pos = self.log.peak_position("all")
        for i in range(3):
            self.assertAlmostEqual(pos["mean_ecef_m"][i], offset(u=0.015)[i], delta=1e-4)
        self.assertAlmostEqual(pos["sd_enu_m"][2], 0.0129, delta=1e-3)

    def test_bad_peak_id(self):
        with self.assertRaises(ValueError):
            self.log.peak_position("x")
        self.add(self.log, T0, SITE)
        with self.assertRaises(ValueError):
            self.log.peak_position("99,99,99")


class TestSeries(LogTestCase):
    def test_bins_stay_bounded_and_keep_min_max(self):
        s = Series(max_bins=10)
        for t in range(1000):
            s.add(t, t, 0, -t)
        self.assertLessEqual(len(s.bins), 10)
        pts = s.points()
        self.assertEqual(sum(p["n"] for p in pts), 1000)
        self.assertAlmostEqual(pts[0]["min"][0], 0.0)
        self.assertAlmostEqual(pts[-1]["max"][0], 999 * locate_log.UNIT_M)

    def test_zoom_reads_full_resolution_from_disk(self):
        for k in range(3000):
            self.add(self.log, T0 + k, offset(u=0.001 * (k // 1000)))
        whole = self.log.analysis()["series"]
        self.assertEqual(whole["width_s"], 4)
        zoom = self.log.series(T0 + 990, T0 + 1009)
        self.assertEqual(zoom["width_s"], 1)
        self.assertEqual(len(zoom["points"]), 20)
        step = [p["mean"][2] for p in zoom["points"]]
        self.assertAlmostEqual(step[-1] - step[0], 0.001, delta=1e-4)


if __name__ == "__main__":
    unittest.main()
