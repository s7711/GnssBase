"""Locate mode's epochs: NAV-PVT and NAV-HPPOSECEF paired into one epoch
each, for locate_log.py to record and average (RTK-fixed epochs only,
NAV-PVT carrSoln == 2).

The carrier solution comes from NAV-PVT and the 0.1 mm position from
NAV-HPPOSECEF, so the two are paired by iTOW before an epoch counts;
the receiver doesn't promise which of the two arrives first.
"""

import math

CARR_SOLN_FIXED = 2


class EpochPairer:
    """Calls `on_epoch(carr_soln, ecef_m, pacc_m, info)` once both NAV-PVT
    and NAV-HPPOSECEF for the same iTOW have arrived. ecef_m is None if
    the receiver flagged the ECEF position invalid; `info` is whatever
    was passed to add_pvt (time, numSV, accuracies)."""

    def __init__(self, on_epoch):
        self._on_epoch = on_epoch
        self._pvt = None  # (iTOW, carrSoln, info)
        self._pos = None  # (iTOW, ecef_m, pacc_m)

    def add_pvt(self, itow: int, carr_soln: int, info=None):
        self._pvt = (itow, carr_soln, info)
        self._try_pair()

    def add_hpposecef(self, itow: int, ecef_m, pacc_m: float):
        self._pos = (itow, ecef_m, pacc_m)
        self._try_pair()

    def _try_pair(self):
        if self._pvt and self._pos and self._pvt[0] == self._pos[0]:
            _, carr_soln, info = self._pvt
            _, ecef_m, pacc_m = self._pos
            self._pvt = self._pos = None
            self._on_epoch(carr_soln, ecef_m, pacc_m, info)


def ecef_to_enu_rotation(ecef_m):
    """Rows are the local east, north and up unit vectors in ECEF. Uses
    geocentric latitude - the ~0.2 degree difference from geodetic is
    irrelevant for rotating an SD."""
    x, y, z = ecef_m
    lon = math.atan2(y, x)
    lat = math.atan2(z, math.hypot(x, y))
    sl, cl = math.sin(lat), math.cos(lat)
    so, co = math.sin(lon), math.cos(lon)
    return [
        [-so, co, 0.0],
        [-sl * co, -sl * so, cl],
        [cl * co, cl * so, sl],
    ]
