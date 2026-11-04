"""Locate's data set: every epoch since "Reset average", kept on disk so
a week-long run survives restarts and power cuts, plus the in-memory
summaries the pages need (overall mean/SD, E/N/U histograms, a bounded
time series and the density peaks).

Positions are E/N/U offsets from a fixed origin - the first fixed
epoch, rounded to the centimetre - in integer 0.1 mm. The origin never
moves during a data set, so the histogram buckets don't either.

Why peaks: over a 39 km baseline the F9P sometimes holds a wrong set of
integer ambiguities while still reporting "fixed" (the solution steps by
centimetres). The right solution keeps coming back to the same place;
wrong ones depend on the satellite geometry, so they move. Each peak is
the epochs within PEAK_RADIUS of a density maximum. If instead the
scatter is one smooth distribution (atmosphere rather than integers),
the overall mean is offered alongside the peaks.

Times are GNSS UTC seconds from NAV-PVT, not the Pi's clock (no RTC).
"""

import json
import logging
import math
import pickle
import struct
import threading
import time
from pathlib import Path

from averager import CARR_SOLN_FIXED, ecef_to_enu_rotation

# t (GNSS UTC, unix s), E, N, U (0.1 mm from the origin), carrSoln, numSV, hAcc, vAcc, pAcc (mm)
RECORD = struct.Struct("<IiiiBBHHH")
INVALID = -(2**31)  # E/N/U for an epoch with no usable position (flagged invalid, or before the origin exists)
UNIT_M = 1e-4
HIST_BIN = 10  # histogram bucket, in 0.1 mm (1 mm): the page merges these into the bucket size it shows
VOXEL = 50  # peak-finding voxel, in 0.1 mm (5 mm)
CHECKPOINT_VERSION = 3  # bump when Aggregates changes: an older checkpoint is ignored and the file replayed
PEAK_RADIUS = 2  # voxels: a peak is the epochs within ~10 mm of its centre
PEAK_SEPARATION = 6  # voxels (30 mm): closer maxima count as the same peak
SERIES_BINS = 1000
ZOOM_MAX_RECORDS = 60000  # zoomed time series read from disk up to this many records; beyond, the in-memory bins
PEAK_CACHE_S = 30
CHECKPOINT_S = 600  # a restart replays only the epochs since the last checkpoint (a full week takes ~80 s on the Zero)


def _neighbourhood(radius):
    r = radius
    return [(a, b, c) for a in range(-r, r + 1) for b in range(-r, r + 1) for c in range(-r, r + 1)
            if a * a + b * b + c * c <= r * r]


class Series:
    """Fixed epochs summarised into time bins covering the whole data set:
    when the span outgrows max_bins, the bin width doubles and neighbours
    merge, so memory and the page's point count stay bounded. Each bin
    keeps count, sum, min and max per axis (a median can't be merged)."""

    def __init__(self, max_bins=SERIES_BINS, width_s=1):
        self.max_bins = max_bins
        self.width = width_s
        self.t0 = None
        self.bins = {}  # index -> [n, sE, sN, sU, minE, minN, minU, maxE, maxN, maxU]

    def add(self, t, e, n, u):
        if self.t0 is None:
            self.t0 = t
        while (t - self.t0) // self.width >= self.max_bins:
            self._halve()
        i = (t - self.t0) // self.width
        b = self.bins.get(i)
        if b is None:
            self.bins[i] = [1, e, n, u, e, n, u, e, n, u]
            return
        b[0] += 1
        b[1] += e
        b[2] += n
        b[3] += u
        if e < b[4]: b[4] = e
        if n < b[5]: b[5] = n
        if u < b[6]: b[6] = u
        if e > b[7]: b[7] = e
        if n > b[8]: b[8] = n
        if u > b[9]: b[9] = u

    def _halve(self):
        merged = {}
        for i, b in self.bins.items():
            m = merged.get(i // 2)
            merged[i // 2] = list(b) if m is None else _merge(m, b)
        self.bins = merged
        self.width *= 2

    def points(self, t_from=None, t_to=None):
        out = []
        for i in sorted(self.bins):
            t = self.t0 + i * self.width
            if (t_from is not None and t + self.width <= t_from) or (t_to is not None and t > t_to):
                continue
            out.append(_point(t, self.bins[i]))
        return out


def _merge(a, b):
    return [a[0] + b[0], a[1] + b[1], a[2] + b[2], a[3] + b[3],
            min(a[4], b[4]), min(a[5], b[5]), min(a[6], b[6]),
            max(a[7], b[7]), max(a[8], b[8]), max(a[9], b[9])]


def _point(t, b):
    n = b[0]
    return {"t": t, "n": n, "mean": [b[1] / n * UNIT_M, b[2] / n * UNIT_M, b[3] / n * UNIT_M],
            "min": [b[4] * UNIT_M, b[5] * UNIT_M, b[6] * UNIT_M], "max": [b[7] * UNIT_M, b[8] * UNIT_M, b[9] * UNIT_M]}


class Aggregates:
    """Everything derived from the records, updated one record at a time.
    Integer sums in 0.1 mm, so nothing loses precision over a week."""

    def __init__(self):
        self.fixed = 0
        self.not_fixed = 0
        self.first_t = None
        self.last_t = None
        self.s = [0, 0, 0]
        self.ss = [0, 0, 0, 0, 0, 0]  # EE, NN, UU, EN, EU, NU
        self.pacc_sum = 0
        self.hist = [{}, {}, {}]
        self.voxels = {}  # (i, j, k) -> [n, sE, sN, sU, sEE, sNN, sUU, sPacc, first_t, last_t]
        self.series = Series()

    def add(self, rec):
        t, e, n, u, carr, _nsv, _hacc, _vacc, pacc = rec
        if self.first_t is None:
            self.first_t = t
        self.last_t = t
        if carr != CARR_SOLN_FIXED or e == INVALID:
            self.not_fixed += 1
            return
        self.fixed += 1
        s, ss = self.s, self.ss
        s[0] += e
        s[1] += n
        s[2] += u
        ss[0] += e * e
        ss[1] += n * n
        ss[2] += u * u
        ss[3] += e * n
        ss[4] += e * u
        ss[5] += n * u
        self.pacc_sum += pacc
        h = self.hist
        k = e // HIST_BIN
        h[0][k] = h[0].get(k, 0) + 1
        k = n // HIST_BIN
        h[1][k] = h[1].get(k, 0) + 1
        k = u // HIST_BIN
        h[2][k] = h[2].get(k, 0) + 1
        key = (e // VOXEL, n // VOXEL, u // VOXEL)
        v = self.voxels.get(key)
        if v is None:
            v = self.voxels[key] = [0, 0, 0, 0, 0, 0, 0, 0, t, t]
        v[0] += 1
        v[1] += e
        v[2] += n
        v[3] += u
        v[4] += e * e
        v[5] += n * n
        v[6] += u * u
        v[7] += pacc
        v[9] = t
        self.series.add(t, e, n, u)

    def enu_stats(self):
        """(mean ENU m, 3x3 ENU covariance m^2 or None) over all fixed epochs."""
        n = self.fixed
        if not n:
            return None, None
        mean = [x / n * UNIT_M for x in self.s]
        if n < 2:
            return mean, None
        s, ss = self.s, self.ss
        idx = {(0, 0): 0, (1, 1): 1, (2, 2): 2, (0, 1): 3, (0, 2): 4, (1, 2): 5}
        cov = [[0.0] * 3 for _ in range(3)]
        for (i, j), k in idx.items():
            c = (ss[k] - s[i] * s[j] / n) / (n - 1) * UNIT_M * UNIT_M
            cov[i][j] = cov[j][i] = c
        return mean, cov

    def peaks(self, count=5):
        vox = self.voxels
        hood = _neighbourhood(PEAK_RADIUS)
        density = {}
        for (i, j, k) in vox:
            total = 0
            for a, b, c in hood:
                v = vox.get((i + a, j + b, k + c))
                if v:
                    total += v[0]
            density[(i, j, k)] = total
        chosen = []
        sep2 = PEAK_SEPARATION * PEAK_SEPARATION
        for key in sorted(density, key=density.get, reverse=True):
            if all(sum((key[d] - c[d]) ** 2 for d in range(3)) > sep2 for c in chosen):
                chosen.append(key)
                if len(chosen) == count:
                    break
        return [self.peak_stats(key) for key in chosen]

    def peak_stats(self, key):
        """Stats of the epochs within PEAK_RADIUS voxels of `key`."""
        n = 0
        s = [0, 0, 0]
        ss = [0, 0, 0]
        pacc = 0
        first = last = None
        for a, b, c in _neighbourhood(PEAK_RADIUS):
            v = self.voxels.get((key[0] + a, key[1] + b, key[2] + c))
            if not v:
                continue
            n += v[0]
            for d in range(3):
                s[d] += v[1 + d]
                ss[d] += v[4 + d]
            pacc += v[7]
            first = v[8] if first is None else min(first, v[8])
            last = v[9] if last is None else max(last, v[9])
        if not n:
            return None
        mean = [x / n * UNIT_M for x in s]
        sd = [math.sqrt(max((ss[d] - s[d] * s[d] / n) / (n - 1), 0.0)) * UNIT_M if n > 1 else None for d in range(3)]
        return {
            "id": ",".join(map(str, key)),
            "epochs": n,
            "share": n / self.fixed if self.fixed else 0.0,
            "mean_enu_m": mean,
            "sd_enu_m": sd,
            "mean_pacc_m": pacc / n / 1000,
            "first_t": first,
            "last_t": last,
        }


def enu_to_ecef(origin, rot, enu):
    return [origin[i] + sum(rot[k][i] * enu[k] for k in range(3)) for i in range(3)]


def read_records(path, start=0, stop=None):
    """Records [start, stop) from the file, by index."""
    size = RECORD.size
    with open(path, "rb") as f:
        f.seek(start * size)
        data = f.read(None if stop is None else (stop - start) * size)
    data = data[: len(data) - len(data) % size]
    return list(RECORD.iter_unpack(data))


class LocateLog:
    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.meta_path = self.dir / "locate.json"
        self.data_path = self.dir / "locate-epochs.bin"
        self.checkpoint_path = self.dir / "locate-checkpoint.pickle"
        self._lock = threading.RLock()
        self._file = None
        self.origin = None
        self.rot = None
        self.agg = Aggregates()
        self.records = 0
        self.loading = False
        self._pending = []
        self._peak_cache = (0.0, None)
        self._generation = 0  # bumped by reset(), so a load still running for the old data set is discarded
        self._checkpoint_at = time.monotonic()

    # -- start-up --

    def load(self):
        """Rebuild the aggregates from disk. Slow for a long data set on
        the Zero, so start() runs it on its own thread; epochs arriving
        meanwhile are written straight away and applied afterwards."""
        with self._lock:
            self.loading = True
            generation = self._generation
            self.dir.mkdir(parents=True, exist_ok=True)
            meta = self._read_meta()
            if meta:
                self.origin = meta["origin_ecef_m"]
                self.rot = ecef_to_enu_rotation(self.origin)
            size = RECORD.size
            length = self.data_path.stat().st_size if self.data_path.exists() else 0
            if length % size:  # a write cut short by a power cut
                logging.warning("[locate] Dropping a partial record at the end of %s", self.data_path)
                with open(self.data_path, "r+b") as f:
                    f.truncate(length - length % size)
            count = length // size
            self._file = open(self.data_path, "ab")
        started = time.monotonic()
        agg, done = self._read_checkpoint(meta, count)
        chunk = 20000
        for start in range(done, count, chunk):
            for rec in read_records(self.data_path, start, min(start + chunk, count)):
                agg.add(rec)
        with self._lock:
            if generation != self._generation:
                return  # reset() while loading: the data set loaded here is gone
            for rec in self._pending:
                agg.add(rec)
            self.records = count + len(self._pending)
            self._pending = []
            self.agg = agg
            self.loading = False
            self._peak_cache = (0.0, None)
        if count:
            logging.info("[locate] Loaded %d epochs (%d fixed, %d from the checkpoint) in %.1f s",
                         count, agg.fixed, done, time.monotonic() - started)

    def start(self):
        threading.Thread(target=self.load, daemon=True, name="locate-load").start()

    def _read_checkpoint(self, meta, count):
        """(aggregates, records they cover), or a fresh start if the
        checkpoint is missing or belongs to another data set."""
        try:
            with open(self.checkpoint_path, "rb") as f:
                saved = pickle.load(f)
            if (saved.get("version") == CHECKPOINT_VERSION and saved["started_t"] == (meta or {}).get("started_t")
                    and saved["records"] <= count):
                return saved["agg"], saved["records"]
        except FileNotFoundError:
            pass
        except Exception as e:  # corrupt or from an older version: just replay everything
            logging.warning("[locate] Ignoring the checkpoint: %s", e)
        return Aggregates(), 0

    def _write_checkpoint(self):
        meta = self._read_meta() or {}
        tmp = self.checkpoint_path.with_suffix(".tmp")
        with open(tmp, "wb") as f:
            pickle.dump({"version": CHECKPOINT_VERSION, "started_t": meta.get("started_t"), "records": self.records,
                         "agg": self.agg}, f)
        tmp.replace(self.checkpoint_path)
        self._checkpoint_at = time.monotonic()

    def _read_meta(self):
        try:
            return json.loads(self.meta_path.read_text())
        except (FileNotFoundError, ValueError):
            return None

    # -- recording --

    def add_epoch(self, t, carr_soln, ecef_m, pacc_m, num_sv=0, h_acc_m=0.0, v_acc_m=0.0):
        with self._lock:
            if self._file is None:
                return  # not loaded yet
            if self.origin is None and carr_soln == CARR_SOLN_FIXED and ecef_m is not None:
                self._set_origin(ecef_m, t)
            if self.origin is None or ecef_m is None:
                e = n = u = INVALID
            else:
                d = [ecef_m[i] - self.origin[i] for i in range(3)]
                e, n, u = (round(sum(self.rot[k][i] * d[i] for i in range(3)) / UNIT_M) for k in range(3))
            rec = (int(t), e, n, u, carr_soln, min(num_sv, 255), _mm(h_acc_m), _mm(v_acc_m), _mm(pacc_m))
            self._file.write(RECORD.pack(*rec))
            self._file.flush()
            if self.loading:
                self._pending.append(rec)
            else:
                self.records += 1
                self.agg.add(rec)
                if time.monotonic() - self._checkpoint_at > CHECKPOINT_S:
                    self._write_checkpoint()

    def _set_origin(self, ecef_m, t):
        self.origin = [round(v, 2) for v in ecef_m]
        self.rot = ecef_to_enu_rotation(self.origin)
        tmp = self.meta_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "_comment": "Locate data set origin - the base antenna's position to ~1 cm. Never commit.",
            "origin_ecef_m": self.origin, "started_t": t}))
        tmp.replace(self.meta_path)

    def reset(self):
        """Start a new data set. The previous one is kept as *.prev, in
        case the reset was a mistake."""
        with self._lock:
            if self._file:
                self._file.close()
            self.dir.mkdir(parents=True, exist_ok=True)
            self._generation += 1
            self.loading = False
            for path in (self.data_path, self.meta_path, self.checkpoint_path):
                if path.exists():
                    path.replace(path.with_name(path.name + ".prev"))
            self.origin = self.rot = None
            self.agg = Aggregates()
            self.records = 0
            self._pending = []
            self._peak_cache = (0.0, None)
            self._file = open(self.data_path, "ab")
        logging.info("[locate] Data set reset")

    # -- reading --

    def summary(self) -> dict:
        """Cheap: the status page asks every second."""
        with self._lock:
            agg = self.agg
            mean_enu, cov = agg.enu_stats()
            mean_ecef = sd_ecef = sd_enu = sd_3d = None
            if mean_enu is not None:
                mean_ecef = enu_to_ecef(self.origin, self.rot, mean_enu)
            if cov is not None:
                sd_enu = [math.sqrt(max(cov[i][i], 0.0)) for i in range(3)]
                r = self.rot
                sd_ecef = [math.sqrt(max(sum(r[a][i] * cov[a][b] * r[b][i] for a in range(3) for b in range(3)), 0.0))
                           for i in range(3)]
                sd_3d = math.sqrt(sum(cov[i][i] for i in range(3)))
            return {
                "loading": self.loading,
                "epochs_fixed": agg.fixed,
                "epochs_not_fixed": agg.not_fixed,
                "elapsed_s": (agg.last_t - agg.first_t) if agg.first_t is not None else 0,
                "first_t": agg.first_t,
                "last_t": agg.last_t,
                "mean_ecef_m": mean_ecef,
                "mean_enu_m": mean_enu,
                "sd_ecef_m": sd_ecef,
                "sd_enu_m": sd_enu,
                "sd_3d_m": sd_3d,
                "mean_pacc_m": agg.pacc_sum / agg.fixed / 1000 if agg.fixed else None,
            }

    def peaks(self):
        with self._lock:
            at, cached = self._peak_cache
            if cached is None or time.monotonic() - at > PEAK_CACHE_S:
                cached = self.agg.peaks()
                self._peak_cache = (time.monotonic(), cached)
            return cached

    def analysis(self) -> dict:
        with self._lock:
            agg = self.agg
            return {
                "summary": self.summary(),
                "hist_bin_m": HIST_BIN * UNIT_M,
                "hist": [sorted(h.items()) for h in agg.hist],
                "peaks": self.peaks(),
                "overall": self._overall(),
                "series": {"width_s": agg.series.width, "points": agg.series.points()},
            }

    def _overall(self):
        """All fixed epochs as one candidate, alongside the peaks - better
        than any peak when the scatter is one smooth distribution."""
        s = self.summary()
        if not s["epochs_fixed"]:
            return None
        return {"id": "all", "epochs": s["epochs_fixed"], "share": 1.0, "mean_enu_m": s["mean_enu_m"],
                "sd_enu_m": s["sd_enu_m"] or [None] * 3, "sd_ecef_m": s["sd_ecef_m"], "mean_pacc_m": s["mean_pacc_m"],
                "mean_ecef_m": s["mean_ecef_m"], "first_t": s["first_t"], "last_t": s["last_t"]}

    def series(self, t_from, t_to) -> dict:
        """The time series between two times: full resolution from disk
        when the range is short enough, else the in-memory bins."""
        with self._lock:
            records = self.records
            coarse = self.agg.series
        lo, hi = self._find(t_from, records), self._find(t_to + 1, records)
        if hi - lo > ZOOM_MAX_RECORDS:
            with self._lock:
                return {"width_s": coarse.width, "points": coarse.points(t_from, t_to)}
        span = max(t_to - t_from, 1)
        width = 1
        while span // width >= SERIES_BINS:
            width *= 2
        s = Series(max_bins=SERIES_BINS + 1, width_s=width)
        s.t0 = t_from - t_from % width
        for t, e, n, u, carr, *_ in read_records(self.data_path, lo, hi):
            if carr == CARR_SOLN_FIXED and e != INVALID and t_from <= t <= t_to:
                s.add(t, e, n, u)
        return {"width_s": s.width, "points": s.points()}

    def _find(self, t, records):
        """Index of the first record at or after time t (records are in time order)."""
        lo, hi = 0, records
        size = RECORD.size
        with open(self.data_path, "rb") as f:
            while lo < hi:
                mid = (lo + hi) // 2
                f.seek(mid * size)
                if RECORD.unpack(f.read(size))[0] < t:
                    lo = mid + 1
                else:
                    hi = mid
        return lo

    def peak_position(self, peak_id: str) -> dict:
        """A peak's stats, recomputed now, with its mean as ECEF. "all" is
        the mean of every fixed epoch."""
        if peak_id == "all":
            with self._lock:
                overall = self._overall()
            if overall is None or None in overall["sd_enu_m"]:
                raise ValueError("Not enough fixed epochs yet")
            return overall
        try:
            key = tuple(int(x) for x in peak_id.split(","))
            assert len(key) == 3
        except (ValueError, AssertionError, AttributeError):
            raise ValueError(f"bad peak id {peak_id!r}")
        with self._lock:
            stats = self.agg.peak_stats(key)
            if stats is None:
                raise ValueError("That peak has no epochs")
            stats["mean_ecef_m"] = enu_to_ecef(self.origin, self.rot, stats["mean_enu_m"])
            r = self.rot
            stats["sd_ecef_m"] = [math.sqrt(sum((r[k][i] * stats["sd_enu_m"][k]) ** 2 for k in range(3)))
                                  if None not in stats["sd_enu_m"] else None for i in range(3)]
            return stats


def _mm(metres):
    return max(0, min(65535, round((metres or 0.0) * 1000)))
