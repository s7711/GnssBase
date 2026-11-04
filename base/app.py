"""GnssBase base service: owns the F9P, runs it in one of three modes,
and serves the status page, the NTRIP caster and the status feed.

- Locate: rover + NTRIP client (OXTS1), averaging RTK-fixed epochs to
  find the antenna's position in the corrections' frame.
- Survey-in: the F9P's own survey-in, for bench tests only. Never saved
  as the base position.
- Base: fixed at the stored position, RTCM out to the caster.

See gnssbase-prd.md. Mode changes and receiver (re)configuration all go
through one control thread, so they never overlap.
"""

import calendar
import json
import logging
import math
import queue
import sys
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, request
from flask_sock import Sock
from pyubx2 import POLL, POLL_LAYER_RAM, UBXMessage, UBXMessageError, UBXParseError, UBXReader, UBXTypeError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.config import load_config  # noqa: E402
from shared.feed_server import FeedServer  # noqa: E402
from shared.web import manager_url, register_pages, service_url, use_shared_static, use_shared_templates  # noqa: E402

from averager import EpochPairer  # noqa: E402
from caster import Caster  # noqa: E402
from framing import NMEA, RTCM, UBX, StreamFramer, rtcm_type  # noqa: E402
from locate_log import LocateLog  # noqa: E402
from ntrip_client import NtripClient  # noqa: E402
from rates import RateCounter  # noqa: E402
from receiver import Receiver  # noqa: E402
from state import StateStore  # noqa: E402
from status import NAV_SAT_ID, ReceiverStatus, hpposecef_values  # noqa: E402
from ubx_config import BASE, CFG_CLASS, LOCATE, MODES, SURVEY, VALGET_ID, VALSET_ID, apply_config, mode_config, valset  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent
REPO_DIR = BASE_DIR.parent
MODE_LABELS = {LOCATE: "Locate", SURVEY: "Survey-in (bench test)", BASE: "Base"}
TMODE_READBACK_KEYS = [
    "CFG_TMODE_MODE", "CFG_TMODE_POS_TYPE", "CFG_TMODE_ECEF_X", "CFG_TMODE_ECEF_Y", "CFG_TMODE_ECEF_Z",
    "CFG_TMODE_ECEF_X_HP", "CFG_TMODE_ECEF_Y_HP", "CFG_TMODE_ECEF_Z_HP",
    "CFG_TMODE_SVIN_MIN_DUR", "CFG_TMODE_SVIN_ACC_LIMIT",
]


class GnssBase:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        data_dir = REPO_DIR / cfg.get("data_dir", "base/data")
        self.state = StateStore(data_dir / "state.yaml")
        self.mode = self.state.mode
        if self.mode == BASE and self.state.position is None:
            self.mode = LOCATE
        self.status = ReceiverStatus()
        self.locate = LocateLog(data_dir)
        self.pairer = EpochPairer(self._on_epoch)
        self.rtcm_out = RateCounter()
        self.rtcm_in = RateCounter()
        self._rtcm_in_framer = StreamFramer()
        self.config_result = None  # last apply: {"mode", "at" (monotonic), "keys", "errors": [...]}
        self.errors = {}  # source -> message, shown on the page
        self._commands = queue.Queue()

        caster_cfg = cfg.get("caster", {})
        self.caster = Caster(
            host=cfg.get("host", "0.0.0.0"),
            port=int(caster_cfg.get("port", 2101)),
            mountpoint=caster_cfg.get("mountpoint", "GNSSBASE"),
            user=caster_cfg.get("user", ""),
            password=caster_cfg.get("password", ""),
            queue_len=int(caster_cfg.get("client_queue_len", 100)),
            max_clients=int(caster_cfg.get("max_clients", 10)),
        )
        self.ntrip = NtripClient(cfg["ntrip_client"], self._on_ntrip_data, data_dir / "ntrip-client.json")
        self.receiver = Receiver(cfg.get("serial_port", "/dev/ttyACM0"), self._on_frame, self._on_receiver_connect)

    def start(self):
        self.locate.start()
        self.caster.start()
        threading.Thread(target=self._control_loop, daemon=True, name="control").start()
        if self.mode == LOCATE:
            self.ntrip.start()
        self.receiver.start()

    # -- commands from the web page; all run on the control thread --

    def request_mode(self, mode: str):
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}")
        if mode == BASE and self.state.position is None:
            raise ValueError("No base position stored yet - run Locate and press 'Use this position' first")
        self._commands.put(("mode", mode))

    def use_located_position(self, peak_id) -> dict:
        """Save the mean of one density peak of the Locate data set (see
        locate_log.py), or of every fixed epoch (peak_id "all"), as the
        base position, and switch to Base."""
        if self.mode != LOCATE:
            raise ValueError("Only available in Locate mode")
        if self.locate.loading:
            raise ValueError("Still loading the Locate data set")
        peak = self.locate.peak_position(peak_id)
        summary = self.locate.summary()
        minimum = int(self.cfg.get("locate", {}).get("min_fixed_epochs", 60))
        if peak["epochs"] < minimum:
            raise ValueError(f"Only {peak['epochs']} epochs; need at least {minimum}")
        record = {
            "ecef_m": [round(v, 5) for v in peak["mean_ecef_m"]],
            "sd_ecef_m": [round(v, 5) for v in peak["sd_ecef_m"]],
            "sd_enu_m": [round(v, 5) for v in peak["sd_enu_m"]],
            "sd_3d_m": round(math.sqrt(sum(v * v for v in peak["sd_enu_m"])), 5),
            "mean_pacc_m": round(peak["mean_pacc_m"], 5),
            "method": "mean of all fixed epochs" if peak_id == "all" else "peak",
            "peak_epochs": peak["epochs"] if peak_id != "all" else None,
            "epochs_fixed": summary["epochs_fixed"],
            "epochs_not_fixed": summary["epochs_not_fixed"],
            "duration_s": round(summary["elapsed_s"]),
            "corrections": f"{self.ntrip.host}/{self.ntrip.mountpoint}",
        }
        saved = self.state.use_position(record)
        logging.info("[base] New base position saved (%s: %d epochs, 3D SD %.4f m)",
                     record["method"], peak["epochs"], record["sd_3d_m"])
        self._commands.put(("mode", BASE))
        return saved

    def reset_average(self):
        self.locate.reset()

    def _control_loop(self):
        while True:
            command, arg = self._commands.get()
            try:
                if command == "mode":
                    self._enter_mode(arg)
                elif command == "configure":
                    self._configure(self.mode)
                    self._poll_version()
            except Exception as e:
                logging.exception("[base] %s %s failed", command, arg)
                self.errors["control"] = f"{command} {arg}: {e}"

    def _enter_mode(self, mode):
        logging.info("[base] Switching mode %s -> %s", self.mode, mode)
        if mode != LOCATE:
            self.ntrip.stop()
        self.mode = mode
        self.state.set_mode(mode)
        self.rtcm_out.reset()
        self._configure(mode)
        if mode == LOCATE:  # the data set carries on: only "Reset average" starts a new one
            self.rtcm_in.reset()
            self.ntrip.start()

    def _configure(self, mode):
        """Send the whole configuration for `mode` to the F9P's RAM layer,
        then read TMODE back."""
        if not self.receiver.connected:
            self.config_result = {"mode": mode, "at": time.monotonic(), "keys": 0, "errors": ["receiver not connected"]}
            return
        items = mode_config(mode, self.cfg, self.state.position)
        results = apply_config(items, lambda chunk: self.receiver.send_cfg(valset(chunk), CFG_CLASS, VALSET_ID))
        errors = [f"{r['key']}={r['value']}: {r['result']}" for r in results if r["result"] != "ACK"]
        for error in errors:
            logging.error("[base] F9P config %s", error)
        logging.info("[base] Configured F9P for %s: %d keys, %d not acknowledged", mode, len(results), len(errors))
        self.config_result = {"mode": mode, "at": time.monotonic(), "keys": len(results), "errors": errors}
        poll = UBXMessage.config_poll(POLL_LAYER_RAM, 0, TMODE_READBACK_KEYS).serialize()
        if self.receiver.send_cfg(poll, CFG_CLASS, VALGET_ID) is not True:
            self.config_result["errors"].append("TMODE readback (CFG-VALGET) not acknowledged")

    def _poll_version(self):
        self.receiver.write(UBXMessage("MON", "MON-VER", POLL).serialize())

    # -- receiver callbacks (reader thread: must not block) --

    def _on_receiver_connect(self):
        self.status.clear()
        self._commands.put(("configure", None))

    def _on_frame(self, kind, frame):
        if kind == RTCM:
            if self.mode in (BASE, SURVEY):
                self.caster.broadcast(frame)
                self.rtcm_out.count(rtcm_type(frame), len(frame))
        elif kind == UBX and frame[2:4] == NAV_SAT_ID:
            self.status.update_nav_sat(frame)
        elif kind == UBX:
            try:
                msg = UBXReader.parse(frame)
            except (UBXMessageError, UBXParseError, UBXTypeError):
                return  # a message pyubx2 doesn't know; the checksum was fine, so not worth logging
            self.status.update(msg)
            if msg.identity == "NAV-PVT":
                self.pairer.add_pvt(msg.iTOW, msg.carrSoln, _pvt_info(msg))
            elif msg.identity == "NAV-HPPOSECEF":
                self.pairer.add_hpposecef(*hpposecef_values(msg))
        elif kind == NMEA and frame[3:6] == b"GGA" and self.mode == LOCATE:
            self.ntrip.send_gga(frame)

    def _on_epoch(self, carr_soln, ecef_m, pacc_m, info):
        if self.mode == LOCATE:
            self.locate.add_epoch(info["t"], carr_soln, ecef_m, pacc_m, info["num_sv"], info["h_acc_m"], info["v_acc_m"])

    def _on_ntrip_data(self, data):
        self.receiver.write(data)
        for kind, frame in self._rtcm_in_framer.feed(data):
            if kind == RTCM:
                self.rtcm_in.count(rtcm_type(frame), len(frame))

    # -- status -- (durations use time.monotonic: the Zero has no RTC, so the wall clock steps at the first NTP sync)

    def snapshot(self) -> dict:
        errors = dict(self.errors)
        if not self.receiver.connected:
            errors["receiver"] = f"F9P not connected on {self.receiver.port}: {self.receiver.last_error}"
        if self.config_result and self.config_result["errors"]:
            errors["config"] = "; ".join(self.config_result["errors"])
        return {
            "time": time.time(),
            "mode": self.mode,
            "mode_label": MODE_LABELS[self.mode],
            "position": self.state.position,
            "receiver": {
                "connected": self.receiver.connected,
                "port": self.receiver.port,
                "bytes_in": self.receiver.bytes_in,
                "discarded_bytes": self.receiver.framer.discarded,
                **self.status.snapshot(),
            },
            "config": self.config_result and {**self.config_result, "age_s": time.monotonic() - self.config_result["at"]},
            "locate": {
                "average": self.locate.summary(),
                "min_fixed_epochs": int(self.cfg.get("locate", {}).get("min_fixed_epochs", 60)),
                "ntrip": self.ntrip.snapshot(),
                "rtcm_in": self.rtcm_in.snapshot(),
            },
            "base": {
                "rtcm_out": self.rtcm_out.snapshot(),
                "caster": self.caster.snapshot(),
            },
            "errors": errors,
        }


def _pvt_info(msg) -> dict:
    """What the Locate log keeps from NAV-PVT. The time is GNSS UTC, not
    the Pi's clock (no RTC); the Pi's clock only if the receiver's isn't valid yet."""
    if msg.validDate and msg.validTime:
        t = calendar.timegm((msg.year, msg.month, msg.day, msg.hour, msg.min, msg.second))
    else:
        t = int(time.time())
    return {"t": t, "num_sv": msg.numSV, "h_acc_m": msg.hAcc / 1000, "v_acc_m": msg.vAcc / 1000}


cfg = load_config()["services"]["base"]
gnss = GnssBase(cfg)

app = Flask(__name__)
use_shared_templates(app)
use_shared_static(app)
sock = Sock(app)


@app.context_processor
def inject_urls():
    browser_host = request.host.split(":")[0]
    return {
        "manager_url": manager_url(browser_host),
        "base_ws_url": service_url(browser_host, "base", scheme="ws") + "/ws/status",
    }


def _positions_context():
    return {"history": gnss.state.history}


register_pages(app, BASE_DIR / "templates" / "pages", index_slug="status",
               context_providers={"positions": _positions_context})


def _api(fn):
    try:
        result = fn()
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "result": result})


@app.post("/api/mode")
def api_mode():
    return _api(lambda: gnss.request_mode((request.get_json(silent=True) or {}).get("mode")))


@app.post("/api/locate/use")
def api_locate_use():
    return _api(lambda: gnss.use_located_position((request.get_json(silent=True) or {}).get("peak")))


@app.get("/api/locate/analysis")
def api_locate_analysis():
    return jsonify({**gnss.locate.analysis(), "mode": gnss.mode,
                    "min_fixed_epochs": int(gnss.cfg.get("locate", {}).get("min_fixed_epochs", 60))})


@app.get("/api/locate/series")
def api_locate_series():
    try:
        t_from, t_to = int(request.args["from"]), int(request.args["to"])
    except (KeyError, ValueError):
        return jsonify({"ok": False, "error": "from and to (unix seconds) are required"}), 400
    return jsonify(gnss.locate.series(t_from, t_to))


@app.post("/api/locate/reset")
def api_locate_reset():
    return _api(gnss.reset_average)


@app.get("/api/status")
def api_status():
    return jsonify(gnss.snapshot())


@sock.route("/ws/status")
def ws_status(ws):
    while True:
        ws.send(json.dumps(gnss.snapshot()))
        time.sleep(1.0)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    gnss.start()
    FeedServer(cfg.get("feed_socket", "/tmp/gnssbase-base-feed.sock"), gnss.snapshot, cfg.get("feed_hz", 1)).start()
    app.run(host=cfg.get("host", "0.0.0.0"), port=cfg["port"], threaded=True)
