"""Race history storage.

The engine hands every finished race to ``RaceStore.save`` as a plain,
JSON-serialisable dict (see ``RaceEngine._result``). For now results live in
memory only and are lost on restart. A persistent backend (e.g. SQLite with a
``races`` table and a ``laps`` table, or one JSON column) only needs to
implement the same two methods and be passed to ``RaceEngine(store=...)``.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Protocol


class RaceStore(Protocol):
    def save(self, result: dict) -> None: ...

    def list_recent(self, limit: int = 20) -> list[dict]: ...


class MemoryRaceStore:
    """Keeps the most recent finished races in memory."""

    def __init__(self, max_races: int = 50):
        self._lock = threading.Lock()
        self._races: deque[dict] = deque(maxlen=max_races)

    def save(self, result: dict) -> None:
        with self._lock:
            self._races.append(result)

    def list_recent(self, limit: int = 20) -> list[dict]:
        with self._lock:
            return list(self._races)[-limit:][::-1]  # newest first
