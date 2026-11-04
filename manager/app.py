"""GnssBase manager: home icon-grid, services status/control page, config
editor, and the one reader of the Pi's own health (/ws/system).

Copied from GR6-v2's manager/app.py, minus the nav/aruco/drive header
badges and power-off's drive cutoff (there's no pico here). Units here
are gnssbase-<service>.
"""

import datetime
import json
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

import yaml
from flask import Flask, abort, render_template, request, send_from_directory
from flask_sock import Sock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.config import CONFIG_PATH, load_config  # noqa: E402
from shared.sysstats import snapshot as sysstats_snapshot  # noqa: E402
from shared.web import manager_url, service_url, use_shared_static, use_shared_templates  # noqa: E402

ALLOWED_ACTIONS = {"start", "stop", "restart"}
STATUS_POLL_SECONDS = 2
SYSSTATS_POLL_SECONDS = 3
MANAGER_SERVICE_NAME = "manager"
CONFIG_BACKUP_DIR = Path(__file__).resolve().parent / "config-backup"
JOURNAL_LINES = 200

app = Flask(__name__)
use_shared_templates(app)
use_shared_static(app)
sock = Sock(app)


@app.context_processor
def inject_manager_url():
    browser_host = request.host.split(":")[0]
    return {
        "manager_url": manager_url(browser_host),
        "base_ws_url": service_url(browser_host, "base", scheme="ws") + "/ws/status",  # mode badge - see sysstatus.js
    }


def services() -> dict:
    return load_config()["services"]


def icon_path(name: str) -> str | None:
    service_dir = Path(__file__).resolve().parent.parent / name
    for ext in ("svg", "png"):
        candidate = service_dir / f"icon.{ext}"
        if candidate.exists():
            return f"/icons/{name}/icon.{ext}"
    return None


def unit_status(unit: str) -> str:
    result = subprocess.run(
        ["systemctl", "is-active", unit],
        capture_output=True,
        text=True,
    )
    state = result.stdout.strip()
    if state == "active":
        return "running"
    if state == "failed":
        return "failed"
    return "stopped"


def control_unit(unit: str, action: str) -> None:
    subprocess.run(["sudo", "systemctl", action, unit], check=True)


@app.route("/")
def home():
    browser_host = request.host.split(":")[0]
    tiles = []
    for name, cfg in services().items():
        if name == MANAGER_SERVICE_NAME:
            continue
        tiles.append(
            {
                "name": name,
                "icon": icon_path(name),
                "url": f"http://{browser_host}:{cfg['port']}/" if cfg.get("web_ui") else None,
            }
        )
    return render_template("home.html", tiles=tiles, current_slug="home")


@app.route("/services")
def services_page():
    rows = []
    for name, cfg in services().items():
        rows.append({"name": name, "unit": cfg["unit"], "status": unit_status(cfg["unit"])})
    return render_template("services.html", rows=rows, current_slug="services")


@app.route("/icons/<name>/<filename>")
def icon(name, filename):
    if name not in services():
        abort(404)
    service_dir = Path(__file__).resolve().parent.parent / name
    return send_from_directory(service_dir, filename)


@app.route("/services/<name>/<action>", methods=["POST"])
def services_action(name, action):
    cfg = services().get(name)
    if cfg is None:
        abort(404)
    if action not in ALLOWED_ACTIONS:
        abort(400)
    control_unit(cfg["unit"], action)
    return "", 204


@app.route("/power-off", methods=["POST"])
def power_off():
    """Clean Pi shutdown, so the power can be pulled without risking the
    SD card. Nothing turns it back on - cycle the power. Requires `sudo
    shutdown` permitted for this user (see sudoers-gnssbase-manager)."""
    result = subprocess.run(["sudo", "shutdown", "-h", "now"], capture_output=True, text=True)
    if result.returncode != 0:
        logging.warning("[manager] shutdown command failed: %s", result.stderr.strip())
    return "", 204


def read_journal(unit: str, lines: int) -> str:
    result = subprocess.run(
        ["journalctl", "-u", unit, "-n", str(lines), "--no-pager"],
        capture_output=True,
        text=True,
    )
    return result.stdout or result.stderr


@app.route("/services/<name>/journal")
def journal_page(name):
    cfg = services().get(name)
    if cfg is None:
        abort(404)
    text = read_journal(cfg["unit"], JOURNAL_LINES)
    return render_template(
        "journal.html", name=name, unit=cfg["unit"], text=text, current_slug="services"
    )


@app.route("/config", methods=["GET", "POST"])
def config_page():
    error = None
    config_text = CONFIG_PATH.read_text()
    if request.method == "POST":
        text = request.form["config_text"]
        try:
            yaml.safe_load(text)
        except yaml.YAMLError as e:
            error = f"Not saved — invalid YAML: {e}"
            config_text = text
        else:
            CONFIG_BACKUP_DIR.mkdir(exist_ok=True)
            timestamp = datetime.datetime.now().strftime("%y%m%d_%H%M%S")
            backup_path = CONFIG_BACKUP_DIR / f"config_{timestamp}.yaml"
            backup_path.write_text(config_text)
            CONFIG_PATH.write_text(text)
            config_text = text
    return render_template(
        "config.html", config_text=config_text, error=error, current_slug="config"
    )


@sock.route("/ws/status")
def ws_status(ws):
    while True:
        statuses = {name: unit_status(cfg["unit"]) for name, cfg in services().items()}
        ws.send(json.dumps(statuses))
        time.sleep(STATUS_POLL_SECONDS)


_sysstats_lock = threading.Lock()
_latest_sysstats = None


def _sysstats_sampler_loop():
    """The one and only caller of sysstats_snapshot(): read_cpu_percent()
    diffs against its own previous call, so several callers (one per
    open tab, as GR6 first had it) would trample each other's baseline.
    Every /ws/system connection just relays the latest cached sample."""
    while True:
        _sample_sysstats_once()
        time.sleep(SYSSTATS_POLL_SECONDS)


def _sample_sysstats_once():
    global _latest_sysstats
    snapshot = sysstats_snapshot()
    with _sysstats_lock:
        _latest_sysstats = snapshot


@sock.route("/ws/system")
def ws_system(ws):
    while True:
        with _sysstats_lock:
            snapshot = _latest_sysstats
        if snapshot is not None:
            ws.send(json.dumps(snapshot))
        time.sleep(SYSSTATS_POLL_SECONDS)


if __name__ == "__main__":
    cfg = load_config()["services"]["manager"]
    threading.Thread(target=_sysstats_sampler_loop, daemon=True).start()
    # threaded=True: every page's shared header holds open websockets.
    app.run(host=cfg["host"], port=cfg["port"], threaded=True)
