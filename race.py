"""Race state machine for the RC lap timer.

Pure logic: no GPIO and no HTTP imports. All timing uses an injected
monotonic clock in integer nanoseconds (``time.monotonic_ns`` in production),
so the engine can be unit-tested with a fake clock.

Threads that touch the engine:
  * the GPIO callback thread  -> ``on_gate(ts)``
  * the scheduler thread      -> ``tick()`` / ``wait_for_next_deadline()``
  * HTTP handler threads      -> commands, ``snapshot()``, ``wait_for_change()``
A single Condition (and its lock) guards all state. Every state change bumps
``version`` and notifies waiters; nobody performs network I/O while holding
the lock.

Timing rules:
  * Gate events carry the monotonic timestamp captured at the GPIO edge; that
    timestamp, not the time of processing, is what gets measured.
  * Scheduled transitions (countdown end, max-duration end) are recorded at
    their *scheduled* time, not when the scheduler thread happens to wake up.
  * Race time excludes paused periods. Lap time = difference in race time
    between consecutive passages.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

log = logging.getLogger(__name__)

NS_PER_MS = 1_000_000
NS_PER_S = 1_000_000_000

# After the max-duration deadline the scheduler waits this long before
# finalizing the race, so a passage timestamped just *before* the deadline but
# delivered a little late (GPIO callback latency) is still counted. Passages
# timestamped at or after the deadline are always rejected.
FINALIZE_GRACE_NS = 150 * NS_PER_MS

IDLE = "idle"
COUNTDOWN = "countdown"
ARMED = "armed"
RUNNING = "running"
PAUSED = "paused"
FINISHED = "finished"

START_AUTO = "auto"
START_GATE = "gate"
START_MODES = (START_AUTO, START_GATE)

FINISH_STOPPED = "stopped"
FINISH_TIME_LIMIT = "time_limit"


class CommandError(Exception):
    """A race command is not allowed in the current state."""


def _ns_to_ms(ns: int) -> int:
    """Round nanoseconds to the nearest millisecond."""
    return (ns + NS_PER_MS // 2) // NS_PER_MS


@dataclass(frozen=True)
class RaceSettings:
    start_mode: str = START_AUTO
    countdown_s: float = 5.0
    max_duration_s: float = 0.0  # 0 = no limit
    min_lap_s: float = 3.0  # lockout after a passage; filters multi-trigger passes

    def updated(self, changes: dict) -> "RaceSettings":
        """Return a validated copy with ``changes`` applied (raises ValueError)."""
        if not isinstance(changes, dict):
            raise ValueError("Settings must be a JSON object")
        unknown = set(changes) - set(self.__dataclass_fields__)
        if unknown:
            raise ValueError(f"Unknown setting(s): {', '.join(sorted(unknown))}")
        values = asdict(self)
        values.update(changes)

        mode = values["start_mode"]
        if mode not in START_MODES:
            raise ValueError(f"start_mode must be one of: {', '.join(START_MODES)}")

        def number(name: str, low: float, high: float) -> float:
            value = values[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a number")
            value = float(value)
            if not low <= value <= high:  # also rejects NaN
                raise ValueError(f"{name} must be between {low:g} and {high:g}")
            return value

        return RaceSettings(
            start_mode=mode,
            countdown_s=number("countdown_s", 0, 600),
            max_duration_s=number("max_duration_s", 0, 24 * 3600),
            min_lap_s=number("min_lap_s", 0.1, 600),
        )


@dataclass(frozen=True)
class Lap:
    number: int
    lap_ms: int
    total_ms: int
    monotonic_ns: int
    wall_time: str

    def to_dict(self) -> dict:
        return {
            "number": self.number,
            "lap_ms": self.lap_ms,
            "total_ms": self.total_ms,
            "wall_time": self.wall_time,
        }


class RaceEngine:
    def __init__(
        self,
        settings: Optional[RaceSettings] = None,
        *,
        clock: Callable[[], int] = time.monotonic_ns,
        wall_clock: Callable[[], float] = time.time,
        store=None,
    ):
        self._clock = clock
        self._wall_clock = wall_clock
        self._store = store
        self._cond = threading.Condition(threading.Lock())
        self._settings = settings or RaceSettings()
        self._version = 0
        self._race_id = 0
        self._pending_results: list = []
        self._reset_race_state()

    # ------------------------------------------------------------------ commands

    def start(self) -> None:
        try:
            with self._cond:
                if self._state not in (IDLE, FINISHED):
                    raise CommandError("A race is already in progress")
                now = self._clock()
                self._reset_race_state()
                self._race_id += 1
                self._state = COUNTDOWN
                self._countdown_end_ns = now + int(self._settings.countdown_s * NS_PER_S)
                self._changed()
                self._advance(now)  # a 0 s countdown transitions immediately
        finally:
            self._flush_results()

    def pause(self) -> None:
        try:
            with self._cond:
                now = self._clock()
                self._advance(now)
                if self._state != RUNNING:
                    raise CommandError("The race timer is not running")
                self._accumulated_ns += now - self._segment_start_ns
                self._paused_at_ns = now
                self._state = PAUSED
                self._changed()
        finally:
            self._flush_results()

    def resume(self) -> None:
        with self._cond:
            if self._state != PAUSED:
                raise CommandError("The race is not paused")
            self._segment_start_ns = self._clock()
            self._state = RUNNING
            self._changed()

    def stop(self) -> None:
        try:
            with self._cond:
                now = self._clock()
                self._advance(now)
                if self._state in (COUNTDOWN, ARMED):
                    # The race timer never started: nothing to record.
                    self._reset_race_state()
                    self._changed()
                elif self._state == RUNNING:
                    self._finish(now, FINISH_STOPPED)
                elif self._state == PAUSED:
                    self._finish(self._paused_at_ns, FINISH_STOPPED)
                elif self._state == FINISHED:
                    pass  # e.g. the time limit was reached a moment ago
                else:
                    raise CommandError("No race in progress")
        finally:
            self._flush_results()

    def reset(self) -> None:
        with self._cond:
            if self._state not in (IDLE, FINISHED):
                raise CommandError("Stop the race before clearing lap data")
            self._reset_race_state()
            self._changed()

    def update_settings(self, changes: dict) -> RaceSettings:
        with self._cond:
            if self._state not in (IDLE, FINISHED):
                raise CommandError("Settings cannot be changed during a race")
            self._settings = self._settings.updated(changes)
            self._changed()
            return self._settings

    # -------------------------------------------------------------- gate / time

    def on_gate(self, ts_ns: int) -> str:
        """Handle a beam-broken edge timestamped ``ts_ns`` (monotonic ns).

        Returns "start", "lap" or "ignored:<reason>".
        """
        try:
            with self._cond:
                self._advance(ts_ns)
                result = self._handle_gate(ts_ns)
                self._changed()
                return result
        finally:
            self._flush_results()

    def tick(self, now_ns: Optional[int] = None) -> None:
        """Apply scheduled transitions (countdown end, time limit)."""
        try:
            with self._cond:
                self._advance(self._clock() if now_ns is None else now_ns, FINALIZE_GRACE_NS)
        finally:
            self._flush_results()

    def wait_for_next_deadline(self, max_wait_s: float) -> None:
        """Block until the next scheduled transition, a state change, or max_wait_s."""
        with self._cond:
            timeout = max_wait_s
            deadline = self._next_deadline_ns()
            if deadline is not None:
                timeout = min(timeout, max(0.0, (deadline - self._clock()) / NS_PER_S))
            if timeout > 0:
                self._cond.wait(timeout)

    def wait_for_change(self, version: Optional[int], timeout_s: float) -> int:
        """Block until ``version`` differs from the current one (or timeout)."""
        with self._cond:
            self._cond.wait_for(lambda: self._version != version, timeout_s)
            return self._version

    # ----------------------------------------------------------------- reading

    @property
    def state(self) -> str:
        with self._cond:
            return self._state

    def snapshot(self, include_laps: bool = True) -> dict:
        with self._cond:
            now = self._clock()
            settings = self._settings
            max_ns = int(settings.max_duration_s * NS_PER_S)
            elapsed_ns = self._elapsed_ns(now)
            if max_ns and self._state == RUNNING:
                # Between the deadline and finalization (grace period).
                elapsed_ns = min(elapsed_ns, max_ns)

            remaining_ms = None
            if max_ns and self._state in (RUNNING, PAUSED):
                remaining_ms = _ns_to_ms(max(0, max_ns - elapsed_ns))
            countdown_ms = None
            if self._state == COUNTDOWN:
                countdown_ms = _ns_to_ms(max(0, self._countdown_end_ns - now))

            data = {
                "version": self._version,
                "race_id": self._race_id,
                "state": self._state,
                "settings": asdict(settings),
                "elapsed_ms": _ns_to_ms(elapsed_ns),
                "countdown_remaining_ms": countdown_ms,
                "remaining_ms": remaining_ms,
                "lap_count": len(self._laps),
            }
            if include_laps:
                best = min(self._laps, key=lambda lap: lap.lap_ms, default=None)
                data.update(
                    {
                        "laps": [lap.to_dict() for lap in self._laps],
                        "best_lap": best.to_dict() if best else None,
                        "last_lap": self._laps[-1].to_dict() if self._laps else None,
                        "started_at": self._started_wall,
                        "finished_at": self._finished_wall,
                        "finish_reason": self._finish_reason,
                        "ignored": dict(self._ignored),
                        "last_event": self._last_event,
                    }
                )
            return data

    # ---------------------------------------------------------------- internals
    # All methods below must be called with self._cond held.

    def _reset_race_state(self) -> None:
        self._state = IDLE
        self._countdown_end_ns: Optional[int] = None
        self._armed_at_ns: Optional[int] = None
        self._segment_start_ns: Optional[int] = None
        self._paused_at_ns: Optional[int] = None
        self._accumulated_ns = 0
        self._laps: list[Lap] = []
        # Start of the current lockout window. Updated by every trigger that
        # is not itself suppressed by the lockout, so a passage that began
        # during the countdown or a pause cannot produce a bogus short lap.
        self._lockout_ref_ns: Optional[int] = None
        self._started_wall: Optional[str] = None
        self._finished_wall: Optional[str] = None
        self._finish_reason: Optional[str] = None
        self._ignored: dict[str, int] = {}
        self._last_event: Optional[dict] = None

    def _changed(self) -> None:
        self._version += 1
        self._cond.notify_all()

    def _wall_at(self, mono_ns: int) -> str:
        wall = self._wall_clock() - (self._clock() - mono_ns) / NS_PER_S
        return datetime.fromtimestamp(wall, timezone.utc).isoformat(timespec="milliseconds")

    def _elapsed_ns(self, now: int) -> int:
        if self._state == RUNNING:
            return self._accumulated_ns + max(0, now - self._segment_start_ns)
        if self._state in (PAUSED, FINISHED):
            return self._accumulated_ns
        return 0

    def _deadline_ns(self) -> Optional[int]:
        """Monotonic time at which the race time reaches max duration."""
        max_ns = int(self._settings.max_duration_s * NS_PER_S)
        if self._state != RUNNING or not max_ns:
            return None
        return self._segment_start_ns + (max_ns - self._accumulated_ns)

    def _next_deadline_ns(self) -> Optional[int]:
        if self._state == COUNTDOWN:
            return self._countdown_end_ns
        deadline = self._deadline_ns()
        return None if deadline is None else deadline + FINALIZE_GRACE_NS

    def _advance(self, now: int, grace_ns: int = 0) -> None:
        """Apply every scheduled transition that is due at ``now``."""
        if self._state == COUNTDOWN and now >= self._countdown_end_ns:
            at = self._countdown_end_ns
            if self._settings.start_mode == START_AUTO:
                self._start_running(at)
            else:
                self._state = ARMED
                self._armed_at_ns = at
            self._changed()
        deadline = self._deadline_ns()
        if deadline is not None and now >= deadline + grace_ns:
            self._finish(deadline, FINISH_TIME_LIMIT)

    def _start_running(self, at_ns: int) -> None:
        self._state = RUNNING
        self._segment_start_ns = at_ns
        self._accumulated_ns = 0
        self._started_wall = self._wall_at(at_ns)

    def _finish(self, at_ns: int, reason: str) -> None:
        if self._state == RUNNING:
            self._accumulated_ns += max(0, at_ns - self._segment_start_ns)
        self._state = FINISHED
        self._finish_reason = reason
        self._finished_wall = self._wall_at(at_ns)
        self._pending_results.append(self._result())
        self._changed()

    def _result(self) -> dict:
        best = min(self._laps, key=lambda lap: lap.lap_ms, default=None)
        return {
            "race_id": self._race_id,
            "started_at": self._started_wall,
            "finished_at": self._finished_wall,
            "finish_reason": self._finish_reason,
            "settings": asdict(self._settings),
            "total_ms": _ns_to_ms(self._accumulated_ns),
            "best_lap_ms": best.lap_ms if best else None,
            "laps": [lap.to_dict() for lap in self._laps],
        }

    def _handle_gate(self, ts: int) -> str:
        state = self._state
        if state in (IDLE, FINISHED):
            return self._ignore(ts, state, update_lockout=False)

        min_lap_ns = int(self._settings.min_lap_s * NS_PER_S)
        if self._lockout_ref_ns is not None and ts - self._lockout_ref_ns < min_lap_ns:
            return self._ignore(ts, "lockout", update_lockout=False)

        if state == COUNTDOWN:
            return self._ignore(ts, "countdown")

        if state == ARMED:
            if ts < self._armed_at_ns:  # happened during the countdown
                return self._ignore(ts, "countdown")
            self._start_running(ts)
            self._lockout_ref_ns = ts
            self._last_event = {"kind": "start", "at": self._started_wall}
            return "start"

        if state == RUNNING:
            if ts < self._segment_start_ns:  # happened before this segment began
                return self._ignore(ts, "paused" if self._paused_at_ns is not None else "countdown")
            return self._record_lap(self._accumulated_ns + ts - self._segment_start_ns, ts)

        if state == PAUSED:
            # A passage that happened just before Pause was pressed but was
            # delivered afterwards still belongs to the race.
            if self._segment_start_ns <= ts < self._paused_at_ns:
                return self._record_lap(self._accumulated_ns - (self._paused_at_ns - ts), ts)
            return self._ignore(ts, "paused")

        return self._ignore(ts, state)  # pragma: no cover - defensive

    def _record_lap(self, total_ns: int, ts: int) -> str:
        total_ms = _ns_to_ms(total_ns)
        previous_ms = self._laps[-1].total_ms if self._laps else 0
        lap = Lap(
            number=len(self._laps) + 1,
            # Derived from rounded totals so lap times always sum to the total.
            lap_ms=total_ms - previous_ms,
            total_ms=total_ms,
            monotonic_ns=ts,
            wall_time=self._wall_at(ts),
        )
        self._laps.append(lap)
        self._lockout_ref_ns = ts
        self._last_event = {"kind": "lap", "lap": lap.number, "at": lap.wall_time}
        return "lap"

    def _ignore(self, ts: int, reason: str, update_lockout: bool = True) -> str:
        if update_lockout:
            self._lockout_ref_ns = ts
        self._ignored[reason] = self._ignored.get(reason, 0) + 1
        self._last_event = {"kind": "ignored", "reason": reason, "at": self._wall_at(ts)}
        return f"ignored:{reason}"

    def _flush_results(self) -> None:
        """Hand finished races to the store, outside the lock."""
        with self._cond:
            pending, self._pending_results = self._pending_results, []
        if self._store is None:
            return
        for result in pending:
            try:
                self._store.save(result)
            except Exception:  # storage must never break timing
                log.exception("Failed to save race result")
