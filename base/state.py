"""What the base app writes for itself, kept apart from the hand-edited
config.yaml (rewriting that with PyYAML would strip its comments, and
the manager's config editor writes it too): the last mode, the base
position in use, and every earlier position with the date it was
replaced.

Lives in base/data/ - gitignored, because the position is Ben's house
to the centimetre.
"""

import datetime
import threading
from pathlib import Path

import yaml

from ubx_config import LOCATE, MODES


def _now_iso():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data = {"mode": LOCATE, "position": None, "history": []}
        try:
            loaded = yaml.safe_load(self.path.read_text()) or {}
            self._data.update(loaded)
        except FileNotFoundError:
            pass
        if self._data.get("mode") not in MODES:
            self._data["mode"] = LOCATE

    @property
    def mode(self) -> str:
        return self._data["mode"]

    @property
    def position(self) -> dict | None:
        return self._data["position"]

    @property
    def history(self) -> list:
        """Every position ever used, newest first; the current one has replaced_at None."""
        return list(self._data["history"])

    def set_mode(self, mode: str):
        if mode not in MODES:
            raise ValueError(mode)
        with self._lock:
            self._data["mode"] = mode
            self._save()

    def use_position(self, record: dict) -> dict:
        """Make `record` (must have ecef_m) the base position, keeping the old one in the history."""
        record = dict(record, saved_at=_now_iso(), replaced_at=None)
        with self._lock:
            for old in self._data["history"]:
                if old.get("replaced_at") is None:
                    old["replaced_at"] = record["saved_at"]
            self._data["history"].insert(0, record)
            self._data["position"] = record
            self._save()
        return record

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            "# Written by the base app - see base/state.py. Holds the base position: never commit.\n"
            + yaml.safe_dump(self._data, sort_keys=False)
        )
        tmp.replace(self.path)
