"""Per-message-type counts and rates, for the RTCM going out to the caster
and coming in from the NTRIP client."""

import collections
import threading
import time


class RateCounter:
    def __init__(self, window_s=30.0, clock=time.monotonic):
        self.window_s = window_s
        self._clock = clock
        self._lock = threading.Lock()
        self._types = {}

    def count(self, msg_type, n_bytes):
        now = self._clock()
        with self._lock:
            entry = self._types.get(msg_type)
            if entry is None:
                entry = self._types[msg_type] = {"count": 0, "bytes": 0, "times": collections.deque()}
            entry["count"] += 1
            entry["bytes"] += n_bytes
            entry["times"].append(now)
            entry["last"] = now
            self._prune(entry["times"], now)

    def reset(self):
        with self._lock:
            self._types.clear()

    def _prune(self, times, now):
        while times and now - times[0] > self.window_s:
            times.popleft()

    def snapshot(self) -> list[dict]:
        """One row per type, sorted by type. rate_hz is over the last window_s."""
        now = self._clock()
        rows = []
        with self._lock:
            for msg_type, entry in sorted(self._types.items()):
                self._prune(entry["times"], now)
                rows.append({
                    "type": msg_type,
                    "count": entry["count"],
                    "bytes": entry["bytes"],
                    "rate_hz": len(entry["times"]) / self.window_s,
                    "age_s": now - entry["last"],
                })
        return rows
