"""Local JSON persistence for the paper wallet and the event log."""

from __future__ import annotations

import threading
import time
from typing import Any

from .datafeed import read_json, write_json


class JsonStore:
    """Small write-through store so restarts keep the paper portfolio."""

    def __init__(self, path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def load(self, default: Any = None) -> Any:
        with self._lock:
            return read_json(self.path, default)

    def save(self, value: Any) -> None:
        with self._lock:
            write_json(self.path, value)

    def append_event(self, events_path, kind: str, message: str, limit: int = 120) -> list[dict]:
        entry = {"at": int(time.time() * 1000), "kind": kind, "message": message}
        events = read_json(events_path, []) or []
        events.append(entry)
        events = events[-limit:]
        write_json(events_path, events)
        return events
