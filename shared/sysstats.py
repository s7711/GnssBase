"""Host-level system health for the Pi itself: under-voltage, throttling,
CPU temperature and load, wifi signal, uptime. Read once by the manager
and served on its /ws/system, which every service's shared header (and
the base status page's "Pi" card) watches cross-port. There's only one
Pi, so one reader is enough. Copied from GR6-v2's shared/sysstats.py,
minus its robot-specific bits.
"""

import subprocess
import time
from pathlib import Path

_WIRELESS_PROC = Path("/proc/net/wireless")
_UPTIME_PROC = Path("/proc/uptime")

_prev_cpu_total = None
_prev_cpu_idle = None


def read_throttled() -> int | None:
    """vcgencmd get_throttled's raw bit field (bit 0 under-voltage now,
    bit 2 throttled now, bit 16 under-voltage since boot, ...), or None
    if vcgencmd isn't available. The page shows it as hex - a non-zero
    value after a hot-plug or with a poor micro-USB cable is the thing
    to look for (see gnssbase-prd.md)."""
    try:
        out = subprocess.run(
            ["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=2
        ).stdout.strip()
        return int(out.split("=")[1], 16)
    except (FileNotFoundError, IndexError, ValueError, subprocess.SubprocessError):
        return None


def read_temp_c() -> float | None:
    try:
        out = subprocess.run(
            ["vcgencmd", "measure_temp"], capture_output=True, text=True, timeout=2
        ).stdout.strip()
        return float(out.split("=")[1].split("'")[0])
    except (FileNotFoundError, IndexError, ValueError, subprocess.SubprocessError):
        return None


def read_wifi(proc_text: str | None = None) -> dict:
    """{"quality_percent": ..., "level_dbm": ...} from the first interface
    in /proc/net/wireless (link quality is out of 70; level is dBm on the
    Zero's brcmfmac). Both None if there's no wireless client up."""
    try:
        text = proc_text if proc_text is not None else _WIRELESS_PROC.read_text()
    except FileNotFoundError:
        return {"quality_percent": None, "level_dbm": None}
    for line in text.splitlines()[2:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        quality = float(parts[2].rstrip("."))
        level = float(parts[3].rstrip("."))
        return {
            "quality_percent": max(0.0, min(100.0, quality / 70 * 100)),
            "level_dbm": level if level < 0 else None,
        }
    return {"quality_percent": None, "level_dbm": None}


def read_uptime_s() -> float | None:
    try:
        return float(_UPTIME_PROC.read_text().split()[0])
    except (FileNotFoundError, IndexError, ValueError):
        return None


def read_cpu_percent() -> float | None:
    """CPU load %, averaged since the previous call. None on the very
    first call (no prior sample yet to diff against)."""
    global _prev_cpu_total, _prev_cpu_idle
    try:
        with open("/proc/stat") as f:
            fields = [int(x) for x in f.readline().split()[1:]]
    except (FileNotFoundError, ValueError):
        return None
    idle = fields[3]
    total = sum(fields)
    percent = None
    if _prev_cpu_total is not None and total > _prev_cpu_total:
        d_total = total - _prev_cpu_total
        d_idle = idle - _prev_cpu_idle
        percent = round((1 - d_idle / d_total) * 100, 1)
    _prev_cpu_total, _prev_cpu_idle = total, idle
    return percent


def snapshot() -> dict:
    return {
        "throttled": read_throttled(),
        "temp_c": read_temp_c(),
        "wifi": read_wifi(),
        "cpu_percent": read_cpu_percent(),
        "uptime_s": read_uptime_s(),
        "time": time.time(),
    }
