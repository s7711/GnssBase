import tempfile
import unittest
from pathlib import Path

from rates import RateCounter
from state import StateStore


class TestStateStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "data" / "state.yaml"

    def tearDown(self):
        self.tmp.cleanup()

    def test_fresh_state_starts_in_locate_with_no_position(self):
        store = StateStore(self.path)
        self.assertEqual(store.mode, "locate")
        self.assertIsNone(store.position)

    def test_mode_persists(self):
        StateStore(self.path).set_mode("base")
        self.assertEqual(StateStore(self.path).mode, "base")

    def test_history_keeps_replaced_positions(self):
        store = StateStore(self.path)
        first = store.use_position({"ecef_m": [1.0, 2.0, 3.0], "sd_3d_m": 0.01})
        second = store.use_position({"ecef_m": [1.5, 2.0, 3.0], "sd_3d_m": 0.005})
        reloaded = StateStore(self.path)
        self.assertEqual(reloaded.position["ecef_m"], [1.5, 2.0, 3.0])
        self.assertEqual([h["ecef_m"] for h in reloaded.history], [[1.5, 2.0, 3.0], [1.0, 2.0, 3.0]])
        self.assertEqual(reloaded.history[1]["replaced_at"], second["saved_at"])
        self.assertIsNone(reloaded.history[0]["replaced_at"])
        self.assertIsNotNone(first["saved_at"])

    def test_bad_mode_in_file_falls_back_to_locate(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("mode: banana\n")
        self.assertEqual(StateStore(self.path).mode, "locate")


class TestRateCounter(unittest.TestCase):
    def test_rate_over_window(self):
        now = [0.0]
        counter = RateCounter(window_s=10, clock=lambda: now[0])
        for t in range(30):
            now[0] = float(t)
            counter.count(1074, 100)
            if t % 10 == 0:
                counter.count(1005, 25)
        rows = {r["type"]: r for r in counter.snapshot()}
        self.assertEqual(rows[1074]["count"], 30)
        self.assertEqual(rows[1074]["bytes"], 3000)
        self.assertAlmostEqual(rows[1074]["rate_hz"], 1.0, delta=0.11)
        self.assertAlmostEqual(rows[1005]["rate_hz"], 0.1, delta=0.11)
        now[0] = 100.0
        self.assertEqual({r["type"]: r["rate_hz"] for r in counter.snapshot()}, {1005: 0.0, 1074: 0.0})
