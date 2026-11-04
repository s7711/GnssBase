"""The receiver's own view of things, from its UBX status messages, kept
as plain dicts for the web page and the feed."""

import struct
import threading
import time

FIX_TYPES = {0: "No fix", 1: "Dead reckoning", 2: "2D", 3: "3D", 4: "GNSS + DR", 5: "Time only"}
CARR_SOLN = {0: "None", 1: "Float", 2: "Fixed"}
TMODES = {0: "Disabled", 1: "Survey-in", 2: "Fixed"}
ANT_STATUS = {0: "Init", 1: "Unknown", 2: "OK", 3: "Short", 4: "Open"}
ANT_POWER = {0: "Off", 1: "On", 2: "Unknown"}
JAMMING = {0: "Unknown", 1: "OK", 2: "Warning", 3: "Critical"}
RF_BLOCKS = {0: "L1", 1: "L2/L5"}
CORRECTION_AGE = {
    0: None, 1: "<1 s", 2: "1-2 s", 3: "2-5 s", 4: "5-10 s", 5: "10-15 s", 6: "15-20 s",
    7: "20-30 s", 8: "30-45 s", 9: "45-60 s", 10: "60-90 s", 11: "90-120 s", 12: ">=120 s",
}
GNSS_NAMES = {0: "GPS", 1: "SBAS", 2: "Galileo", 3: "BeiDou", 4: "IMES", 5: "QZSS", 6: "GLONASS", 7: "NavIC"}
GNSS_ORDER = ["GPS", "GLONASS", "Galileo", "BeiDou", "QZSS", "SBAS", "NavIC", "IMES"]
AGC_MAX = 8191


def _text(value) -> str:
    if isinstance(value, bytes):
        return value.split(b"\x00", 1)[0].decode("ascii", "replace")
    return str(value)


def _repeated(msg, name, count):
    return [getattr(msg, f"{name}_{i:02d}") for i in range(1, count + 1)]


class ReceiverStatus:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self.clear()

    def clear(self):
        with self._lock:
            self._data = {"pvt": None, "hpposecef": None, "sats": None, "rf": None, "svin": None, "version": None, "tmode": None}
            self._times = {}

    def update(self, msg):
        handler = _HANDLERS.get(msg.identity)
        if handler is None:
            return
        self._set(_KEYS[msg.identity], handler(msg))

    def update_nav_sat(self, frame: bytes):
        """NAV-SAT straight from the raw frame - see decode_nav_sat."""
        self._set("sats", decode_nav_sat(frame))

    def _set(self, key, value):
        with self._lock:
            self._data[key] = value
            self._times[key] = self._clock()

    def snapshot(self) -> dict:
        now = self._clock()
        with self._lock:
            out = dict(self._data)
            out["age_s"] = {k: now - t for k, t in self._times.items()}
        return out


def _pvt(msg):
    return {
        "itow": msg.iTOW,
        "fix_type": msg.fixType,
        "fix_type_name": FIX_TYPES.get(msg.fixType, str(msg.fixType)),
        "gnss_fix_ok": bool(msg.gnssFixOk),
        "diff_soln": bool(msg.diffSoln),
        "carr_soln": msg.carrSoln,
        "carr_soln_name": CARR_SOLN.get(msg.carrSoln, str(msg.carrSoln)),
        "num_sv": msg.numSV,
        "h_acc_m": msg.hAcc / 1000,
        "v_acc_m": msg.vAcc / 1000,
        "correction_age": CORRECTION_AGE.get(msg.lastCorrectionAge),
        "utc": f"{msg.year:04d}-{msg.month:02d}-{msg.day:02d} {msg.hour:02d}:{msg.min:02d}:{msg.second:02d}",
    }


def hpposecef_values(msg):
    """(iTOW, ECEF metres or None if flagged invalid, pAcc metres). pyubx2
    has already folded the HP parts into ecefX/Y/Z, in cm."""
    ecef = None if msg.invalidEcef else [msg.ecefX / 100, msg.ecefY / 100, msg.ecefZ / 100]
    return msg.iTOW, ecef, msg.pAcc / 1000


def _hpposecef(msg):
    _, ecef, pacc = hpposecef_values(msg)
    return {"ecef_m": ecef, "p_acc_m": pacc}


NAV_SAT_ID = b"\x01\x35"  # class, id


def decode_nav_sat(frame: bytes):
    """NAV-SAT decoded with struct rather than pyubx2, which takes ~280 ms
    per 40-SV message on the Zero - over a quarter of its CPU at 1 Hz.
    Payload: 8-byte header, then 12 bytes per SV (gnssId, svId, cno,
    elev, azim, prRes, flags; svUsed is flags bit 3)."""
    payload = frame[6:-2]
    num_svs = payload[5]
    rows = []
    for i in range(num_svs):
        gnss, sv, cno, elev, azim, _pr_res, flags = struct.unpack_from("<BBBbhhI", payload, 8 + 12 * i)
        rows.append((gnss, sv, cno, elev, azim, (flags >> 3) & 1))
    return _sat_summary(rows)


def _sat(msg):
    return _sat_summary(zip(
        _repeated(msg, "gnssId", msg.numSvs), _repeated(msg, "svId", msg.numSvs),
        _repeated(msg, "cno", msg.numSvs), _repeated(msg, "elev", msg.numSvs),
        _repeated(msg, "azim", msg.numSvs), _repeated(msg, "svUsed", msg.numSvs),
    ))


def _sat_summary(rows):
    sats = []
    summary = {}
    for gnss, sv, cno, elev, azim, used in rows:
        gnss = GNSS_NAMES.get(gnss, str(gnss))
        sats.append({"gnss": gnss, "sv": sv, "cno": cno, "elev": elev, "azim": azim, "used": bool(used)})
        entry = summary.setdefault(gnss, {"gnss": gnss, "tracked": 0, "used": 0})
        if cno > 0:
            entry["tracked"] += 1
        if used:
            entry["used"] += 1
    order = {g: i for i, g in enumerate(GNSS_ORDER)}
    sats.sort(key=lambda s: (order.get(s["gnss"], 99), s["sv"]))
    return {
        "sats": sats,
        "summary": sorted(summary.values(), key=lambda s: order.get(s["gnss"], 99)),
        "tracked": sum(s["tracked"] for s in summary.values()),
        "used": sum(s["used"] for s in summary.values()),
    }


def _rf(msg):
    blocks = []
    for i in range(1, msg.nBlocks + 1):
        get = lambda name: getattr(msg, f"{name}_{i:02d}")  # noqa: E731
        blocks.append({
            "block": RF_BLOCKS.get(get("blockId"), str(get("blockId"))),
            "ant_status": ANT_STATUS.get(get("antStatus"), str(get("antStatus"))),
            "ant_power": ANT_POWER.get(get("antPower"), str(get("antPower"))),
            "jamming": JAMMING.get(get("jammingState"), str(get("jammingState"))),
            "jam_ind": get("jamInd"),
            "agc_cnt": get("agcCnt"),
            "agc_percent": 100 * get("agcCnt") / AGC_MAX,
            "noise_per_ms": get("noisePerMS"),
        })
    return blocks


def _svin(msg):
    return {
        "duration_s": msg.dur,
        "mean_ecef_m": [(getattr(msg, f"mean{a}") + getattr(msg, f"mean{a}HP") / 100) / 100 for a in "XYZ"],
        "mean_acc_m": msg.meanAcc / 10000,
        "observations": msg.obs,
        "valid": bool(msg.valid),
        "active": bool(msg.active),
    }


def _version(msg):
    extensions = []
    i = 1
    while hasattr(msg, f"extension_{i:02d}"):
        extensions.append(_text(getattr(msg, f"extension_{i:02d}")))
        i += 1
    fw = next((e.split("=", 1)[1] for e in extensions if e.startswith("FWVER=")), None)
    prot = next((e.split("=", 1)[1] for e in extensions if e.startswith("PROTVER=")), None)
    mod = next((e.split("=", 1)[1] for e in extensions if e.startswith("MOD=")), None)
    return {"sw": _text(msg.swVersion), "hw": _text(msg.hwVersion), "fw": fw, "protver": prot, "module": mod,
            "extensions": extensions}


def _valget(msg):
    """CFG-VALGET reply to our TMODE readback poll."""
    out = {}
    for name in ("CFG_TMODE_MODE", "CFG_TMODE_POS_TYPE", "CFG_TMODE_SVIN_MIN_DUR", "CFG_TMODE_SVIN_ACC_LIMIT"):
        if hasattr(msg, name):
            out[name] = getattr(msg, name)
    if "CFG_TMODE_MODE" in out:
        out["mode_name"] = TMODES.get(out["CFG_TMODE_MODE"], str(out["CFG_TMODE_MODE"]))
    if all(hasattr(msg, f"CFG_TMODE_ECEF_{a}") for a in "XYZ"):
        out["ecef_m"] = [
            getattr(msg, f"CFG_TMODE_ECEF_{a}") / 100 + getattr(msg, f"CFG_TMODE_ECEF_{a}_HP", 0) / 10000
            for a in "XYZ"
        ]
    return out


_HANDLERS = {
    "NAV-PVT": _pvt,
    "NAV-HPPOSECEF": _hpposecef,
    "NAV-SAT": _sat,
    "MON-RF": _rf,
    "NAV-SVIN": _svin,
    "MON-VER": _version,
    "CFG-VALGET": _valget,
}
_KEYS = {
    "NAV-PVT": "pvt",
    "NAV-HPPOSECEF": "hpposecef",
    "NAV-SAT": "sats",
    "MON-RF": "rf",
    "NAV-SVIN": "svin",
    "MON-VER": "version",
    "CFG-VALGET": "tmode",
}
