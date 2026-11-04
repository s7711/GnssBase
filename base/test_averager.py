import unittest

from averager import EpochPairer

# Made-up site, 45N 0E on a sphere-ish Earth - not a real location.
SITE = [4517590.879, 0.0, 4487348.409]


class TestEpochPairer(unittest.TestCase):
    def setUp(self):
        self.epochs = []
        self.pairer = EpochPairer(lambda *a: self.epochs.append(a))

    def test_pvt_first(self):
        self.pairer.add_pvt(1000, 2)
        self.pairer.add_hpposecef(1000, SITE, 0.01)
        self.assertEqual(self.epochs, [(2, SITE, 0.01, None)])

    def test_position_first(self):
        self.pairer.add_hpposecef(1000, SITE, 0.01)
        self.pairer.add_pvt(1000, 1)
        self.assertEqual(self.epochs, [(1, SITE, 0.01, None)])

    def test_mismatched_itow_does_not_pair(self):
        self.pairer.add_pvt(1000, 2)
        self.pairer.add_hpposecef(2000, SITE, 0.01)
        self.assertEqual(self.epochs, [])
        self.pairer.add_pvt(2000, 2)
        self.assertEqual(len(self.epochs), 1)

    def test_each_epoch_counted_once(self):
        self.pairer.add_pvt(1000, 2)
        self.pairer.add_hpposecef(1000, SITE, 0.01)
        self.pairer.add_hpposecef(1000, SITE, 0.01)
        self.assertEqual(len(self.epochs), 1)

    def test_pvt_info_is_passed_through(self):
        self.pairer.add_pvt(1000, 2, {"t": 1790000000})
        self.pairer.add_hpposecef(1000, SITE, 0.01)
        self.assertEqual(self.epochs, [(2, SITE, 0.01, {"t": 1790000000})])
