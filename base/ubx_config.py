"""F9P configuration: the key/value list for each mode, and sending it as
CFG-VALSET to the RAM layer with every ACK/NAK checked.

Each mode's list is complete - it sets every key any mode touches - so
switching mode is just applying the new mode's list, and nothing
depends on what was in the receiver before (factory-fresh, swapped, or
left in another mode by a previous run). Nothing is written to BBR or
flash.

A NAKed VALSET applies none of its keys, so on a NAK each key is resent
on its own: the good ones still get applied and the page can name the
ones the firmware rejected (GR6 found HPG 1.50 rejects some settings).
"""

import math
import threading

from pyubx2 import SET_LAYER_RAM, TXN_NONE, UBXMessage

LOCATE = "locate"
SURVEY = "survey"
BASE = "base"
MODES = (LOCATE, SURVEY, BASE)

MSM4_TYPES = (1074, 1084, 1094, 1124)  # GPS, GLONASS, Galileo, BeiDou
NMEA_IDS = ("GGA", "GLL", "GSA", "GSV", "RMC", "VTG")
UBX_STATUS_MSGS = ("NAV_PVT", "NAV_SAT", "NAV_HPPOSECEF", "MON_RF")

TMODE_DISABLED, TMODE_SURVEY_IN, TMODE_FIXED = 0, 1, 2
VALSET_MAX_KEYS = 64  # firmware limit per message

CFG_CLASS, VALSET_ID, VALGET_ID = 0x06, 0x8A, 0x8B


def split_ecef(metres: float) -> tuple[int, int]:
    """Split a coordinate into CFG-TMODE-ECEF_x (cm) and its _HP part
    (0.1 mm, -99..99, same sign), as the receiver adds them back."""
    tenth_mm = round(metres * 10000)
    cm = math.trunc(tenth_mm / 100)
    return cm, tenth_mm - cm * 100


def mode_config(mode: str, cfg: dict, position: dict | None = None) -> list[tuple[str, int]]:
    """Every key for `mode`. `cfg` is config.yaml's base section;
    `position` is the stored base position (needed for BASE only)."""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    rtcm_cfg = cfg.get("rtcm", {})
    serving = mode in (SURVEY, BASE)

    items = [
        ("CFG_RATE_MEAS", 1000),
        ("CFG_RATE_NAV", 1),
        ("CFG_USBINPROT_UBX", 1),
        ("CFG_USBINPROT_RTCM3X", 1),
        ("CFG_USBOUTPROT_UBX", 1),
        ("CFG_USBOUTPROT_NMEA", 1),
        ("CFG_USBOUTPROT_RTCM3X", 1),
        # Not CFG-ITFM: HPG 1.51 NAKs it, so MON-RF's jammingState stays "Unknown"; jamInd still reports.
    ]
    # NMEA: off, except a GGA every gga_interval_s epochs in Locate, for
    # the NTRIP client to forward to the caster.
    gga_rate = int(cfg.get("ntrip_client", {}).get("gga_interval_s", 10))
    for nmea_id in NMEA_IDS:
        rate = gga_rate if (nmea_id == "GGA" and mode == LOCATE) else 0
        items.append((f"CFG_MSGOUT_NMEA_ID_{nmea_id}_USB", rate))
    for name in UBX_STATUS_MSGS:
        items.append((f"CFG_MSGOUT_UBX_{name}_USB", 1))
    items.append(("CFG_MSGOUT_UBX_NAV_SVIN_USB", 1 if mode == SURVEY else 0))

    items.append(("CFG_MSGOUT_RTCM_3X_TYPE1005_USB", int(rtcm_cfg.get("rate_1005", 10)) if serving else 0))
    for msg_type in MSM4_TYPES:
        items.append((f"CFG_MSGOUT_RTCM_3X_TYPE{msg_type}_USB", 1 if serving else 0))
    items.append(("CFG_MSGOUT_RTCM_3X_TYPE1230_USB", int(rtcm_cfg.get("rate_1230", 10)) if serving else 0))

    if mode == LOCATE:
        items.append(("CFG_TMODE_MODE", TMODE_DISABLED))
    elif mode == SURVEY:
        svin = cfg.get("survey_in", {})
        items += [
            ("CFG_TMODE_MODE", TMODE_SURVEY_IN),
            ("CFG_TMODE_SVIN_MIN_DUR", int(svin.get("min_duration_s", 300))),
            ("CFG_TMODE_SVIN_ACC_LIMIT", round(float(svin.get("accuracy_limit_m", 2.0)) * 10000)),
        ]
    else:
        if position is None:
            raise ValueError("Base mode needs a stored base position - run Locate first")
        items += [("CFG_TMODE_MODE", TMODE_FIXED), ("CFG_TMODE_POS_TYPE", 0)]  # 0 = ECEF
        for axis, value in zip("XYZ", position["ecef_m"]):
            cm, hp = split_ecef(value)
            items += [(f"CFG_TMODE_ECEF_{axis}", cm), (f"CFG_TMODE_ECEF_{axis}_HP", hp)]
        acc_m = max(float(position.get("sd_3d_m") or 0.0), 0.01)
        items.append(("CFG_TMODE_FIXED_POS_ACC", round(acc_m * 10000)))
    return items


def valset(items) -> bytes:
    return UBXMessage.config_set(SET_LAYER_RAM, TXN_NONE, list(items)).serialize()


def apply_config(items, send_valset, chunk_size=VALSET_MAX_KEYS) -> list[dict]:
    """Send `items` as VALSETs. `send_valset(chunk)` sends one and returns
    True (ACK), False (NAK) or None (no reply). Returns one result per
    key: {"key", "value", "result": "ACK" | "NAK" | "timeout"}."""
    results = []
    items = list(items)
    for start in range(0, len(items), chunk_size):
        chunk = items[start : start + chunk_size]
        reply = send_valset(chunk)
        if reply is True:
            results += [_result(k, v, "ACK") for k, v in chunk]
        elif reply is False and len(chunk) > 1:
            for key, value in chunk:
                single = send_valset([(key, value)])
                results.append(_result(key, value, "ACK" if single is True else "NAK" if single is False else "timeout"))
        else:
            outcome = "NAK" if reply is False else "timeout"
            results += [_result(k, v, outcome) for k, v in chunk]
    return results


def _result(key, value, outcome):
    return {"key": key, "value": value, "result": outcome}


class AckTracker:
    """Matches UBX-ACK-ACK/NAK to the one outstanding CFG request. ACKs
    carry only the class/id they answer, not a sequence number, so the
    caller must keep one request in flight at a time (Receiver does, with
    its config lock)."""

    def __init__(self):
        self._cond = threading.Condition()
        self._pending = None
        self._result = None

    def expect(self, cls_id: int, msg_id: int):
        with self._cond:
            self._pending = (cls_id, msg_id)
            self._result = None

    def on_ack(self, cls_id: int, msg_id: int, acked: bool):
        with self._cond:
            if self._pending == (cls_id, msg_id) and self._result is None:
                self._result = acked
                self._cond.notify_all()

    def wait(self, timeout: float) -> bool | None:
        with self._cond:
            self._cond.wait_for(lambda: self._result is not None, timeout)
            result = self._result
            self._pending = None
            return result
