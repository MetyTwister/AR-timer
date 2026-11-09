"""Light-curtain timing gate on a Raspberry Pi GPIO input.

Wiring (as built): the light curtain pulls the GPIO line LOW while the beam is
broken; the internal pull-up holds it HIGH while the beam is clear. With
``gpiozero.Button(pull_up=True)`` "pressed" therefore means "beam broken".

Edge timestamps are taken with ``time.monotonic_ns()`` as the very first
statement of the gpiozero callback, before any locking or other work, and
handed to the race engine. Debouncing is intentionally *not* done here
(``bounce_time=None``): gpiozero's software bounce can swallow release edges
and leave its state out of sync. Multi-trigger passages are filtered by the
race engine's minimum-lap lockout instead.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

log = logging.getLogger(__name__)

NS_PER_MS = 1_000_000


class GateSensor:
    def __init__(
        self,
        pin: int,
        on_trigger: Callable[[int], object],
        *,
        stuck_after_s: float = 2.0,
        clock: Callable[[], int] = time.monotonic_ns,
    ):
        self.pin = pin
        self.error: Optional[str] = None
        self._on_trigger = on_trigger
        self._clock = clock
        self._stuck_ns = int(stuck_after_s * 1_000_000_000)
        self._lock = threading.Lock()
        self._trigger_count = 0
        self._last_trigger_ns: Optional[int] = None
        self._broken_since_ns: Optional[int] = None
        self._button = None

        try:
            from gpiozero import Button

            button = Button(pin, pull_up=True, bounce_time=None)
        except Exception as exc:  # Keep the web UI available without GPIO.
            self.error = f"GPIO setup failed: {exc}"
            log.warning(self.error)
            return

        if button.is_pressed:
            self._broken_since_ns = clock()
        button.when_pressed = self._beam_broken
        button.when_released = self._beam_cleared
        self._button = button

    @property
    def available(self) -> bool:
        return self._button is not None

    def _beam_broken(self) -> None:
        ts = self._clock()  # capture first: this is the measured instant
        with self._lock:
            self._trigger_count += 1
            self._last_trigger_ns = ts
            self._broken_since_ns = ts
        try:
            self._on_trigger(ts)
        except Exception:  # never let an error kill the GPIO callback thread
            log.exception("Gate trigger handler failed")

    def _beam_cleared(self) -> None:
        with self._lock:
            self._broken_since_ns = None

    def status(self) -> dict:
        base = {"pin": self.pin, "available": self.available, "error": self.error}
        if self._button is None:
            return {**base, "level": None, "beam": None, "fault": self.error}
        try:
            broken = bool(self._button.is_pressed)
        except Exception as exc:
            return {**base, "available": False, "error": f"GPIO read failed: {exc}",
                    "level": None, "beam": None, "fault": f"GPIO read failed: {exc}"}

        now = self._clock()
        with self._lock:
            if broken and self._broken_since_ns is None:
                self._broken_since_ns = now  # edge missed; start timing from here
            elif not broken:
                self._broken_since_ns = None
            blocked_ns = now - self._broken_since_ns if broken else 0
            count = self._trigger_count
            last = self._last_trigger_ns

        fault = None
        if broken and blocked_ns >= self._stuck_ns:
            fault = (f"Beam blocked for {blocked_ns / 1e9:.0f} s – check sensor "
                     "alignment and wiring")
        return {
            **base,
            "level": "LOW" if broken else "HIGH",
            "beam": "broken" if broken else "clear",
            "trigger_count": count,
            "last_trigger_ago_ms": None if last is None else (now - last) // NS_PER_MS,
            "fault": fault,
        }

    def close(self) -> None:
        if self._button is not None:
            self._button.close()
            self._button = None
