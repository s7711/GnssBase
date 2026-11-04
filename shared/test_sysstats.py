import unittest

import sysstats

WIRELESS = """Inter-| sta-|   Quality        |   Discarded packets               | Missed | WE
 face | tus | link level noise |  nwid  crypt   frag  retry   misc | beacon | 22
 wlan0: 0000   52.  -58.  -256        0      0      0      0      0        0
"""


class TestWifi(unittest.TestCase):
    def test_reads_quality_and_level(self):
        wifi = sysstats.read_wifi(WIRELESS)
        self.assertAlmostEqual(wifi["quality_percent"], 52 / 70 * 100)
        self.assertEqual(wifi["level_dbm"], -58.0)

    def test_no_interface(self):
        wifi = sysstats.read_wifi("\n".join(WIRELESS.splitlines()[:2]))
        self.assertEqual(wifi, {"quality_percent": None, "level_dbm": None})
