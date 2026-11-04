"""Loader for the shared GnssBase config file (see ../config.yaml).

Copied from GR6-v2's shared/config.py."""

from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"

_cache: dict[Path, dict] = {}


def load_config(path: Path = CONFIG_PATH) -> dict:
    """Cached for the life of the process, keyed by path — every service
    already reads config at startup only and never live-reloads (see
    top-prd.md's "Shared configuration" decision/manager-prd.md's "No
    live reload"), so re-parsing the YAML from disk on every call (as
    this used to do) was pure waste, not a freshness guarantee anything
    relied on — found because it was slow enough to be felt as page-load
    lag once a page's header started calling it a few times per request
    (manager_url + oxtsnav_ws_url + aruco_ws_url, each independently)."""
    if path not in _cache:
        with open(path) as f:
            _cache[path] = yaml.safe_load(f)
    return _cache[path]


def service_config(name: str, path: Path = CONFIG_PATH) -> dict:
    return load_config(path)["services"][name]
