"""Unit tests for the race state machine (pure logic, fake clock, no GPIO)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from race import (  # noqa: E402
    ARMED,
    COUNTDOWN,
    FINALIZE_GRACE_NS,
    FINISHED,
    IDLE,
    PAUSED,
    RUNNING,
    CommandError,
    RaceEngine,
    RaceSettings,
)
from store import MemoryRaceStore  # noqa: E402

S = 1_000_000_000  # ns per second


class FakeClock:
    def __init__(self, start_s=1000.0):
        self.now = int(start_s * S)

    def __call__(self):
        return self.now

    def set(self, seconds):
        self.now = int(seconds * S)

    def wall(self):
        return 1_700_000_000.0 + self.now / S


def make(**settings):
    clock = FakeClock()
    store = MemoryRaceStore()
    engine = RaceEngine(
        RaceSettings(**settings), clock=clock, wall_clock=clock.wall, store=store
    )
    return engine, clock, store


def at(clock, seconds):
    """Absolute fake-clock timestamp, relative to the initial 1000 s."""
    return int((1000 + seconds) * S)


class AutoStartTests(unittest.TestCase):
    def test_countdown_then_running_from_scheduled_time(self):
        engine, clock, _ = make(countdown_s=5)
        engine.start()
        self.assertEqual(engine.state, COUNTDOWN)

        engine.tick(at(clock, 4.9))
        self.assertEqual(engine.state, COUNTDOWN)

        # Scheduler wakes late; race must still start at exactly t=5.
        clock.set(1000 + 5.2)
        engine.tick()
        self.assertEqual(engine.state, RUNNING)
        self.assertEqual(engine.snapshot()["elapsed_ms"], 200)

    def test_laps_and_totals(self):
        engine, clock, _ = make(countdown_s=5)
        engine.start()
        engine.tick(at(clock, 5))
        self.assertEqual(engine.on_gate(at(clock, 15)), "lap")
        self.assertEqual(engine.on_gate(at(clock, 27.5)), "lap")
        clock.set(1000 + 28)
        snap = engine.snapshot()
        self.assertEqual([lap["lap_ms"] for lap in snap["laps"]], [10000, 12500])
        self.assertEqual([lap["total_ms"] for lap in snap["laps"]], [10000, 22500])
        self.assertEqual(snap["best_lap"]["number"], 1)
        self.assertEqual(snap["last_lap"]["number"], 2)

    def test_zero_countdown_starts_immediately(self):
        engine, _, _ = make(countdown_s=0)
        engine.start()
        self.assertEqual(engine.state, RUNNING)

    def test_gate_event_processed_late_still_applies_countdown_end(self):
        # The scheduler has not woken yet, but the gate event (after the
        # countdown end) must see the race as running.
        engine, clock, _ = make(countdown_s=5)
        engine.start()
        clock.set(1000 + 20)
        self.assertEqual(engine.on_gate(at(clock, 15)), "lap")
        self.assertEqual(engine.snapshot()["laps"][0]["total_ms"], 10000)

    def test_passage_spanning_countdown_end_is_not_a_lap(self):
        engine, clock, _ = make(countdown_s=5, min_lap_s=3)
        engine.start()
        self.assertEqual(engine.on_gate(at(clock, 4.95)), "ignored:countdown")
        engine.tick(at(clock, 5))
        # Tail of the same passage, after the timer started.
        self.assertEqual(engine.on_gate(at(clock, 5.05)), "ignored:lockout")
        self.assertEqual(engine.on_gate(at(clock, 15)), "lap")
        self.assertEqual(engine.snapshot()["lap_count"], 1)


class GateStartTests(unittest.TestCase):
    def test_first_passage_starts_timer_without_lap(self):
        engine, clock, _ = make(start_mode="gate", countdown_s=3)
        engine.start()
        engine.tick(at(clock, 3))
        self.assertEqual(engine.state, ARMED)

        clock.set(1000 + 7)
        self.assertEqual(engine.snapshot()["elapsed_ms"], 0)
        self.assertEqual(engine.on_gate(at(clock, 8)), "start")
        self.assertEqual(engine.state, RUNNING)
        self.assertEqual(engine.snapshot()["lap_count"], 0)

        self.assertEqual(engine.on_gate(at(clock, 18)), "lap")
        clock.set(1000 + 18)
        snap = engine.snapshot()
        self.assertEqual(snap["laps"][0]["lap_ms"], 10000)
        self.assertEqual(snap["elapsed_ms"], 10000)

    def test_passages_during_countdown_ignored(self):
        engine, clock, _ = make(start_mode="gate", countdown_s=5, min_lap_s=0.5)
        engine.start()
        self.assertEqual(engine.on_gate(at(clock, 1)), "ignored:countdown")
        self.assertEqual(engine.state, COUNTDOWN)
        engine.tick(at(clock, 5))
        self.assertEqual(engine.state, ARMED)
        self.assertEqual(engine.snapshot()["ignored"], {"countdown": 1})

    def test_event_before_arming_delivered_late_is_ignored(self):
        engine, clock, _ = make(start_mode="gate", countdown_s=5, min_lap_s=0.1)
        engine.start()
        engine.tick(at(clock, 5.01))
        self.assertEqual(engine.state, ARMED)
        self.assertEqual(engine.on_gate(at(clock, 4.99)), "ignored:countdown")
        self.assertEqual(engine.state, ARMED)

    def test_start_passage_triggers_lockout(self):
        engine, clock, _ = make(start_mode="gate", countdown_s=0, min_lap_s=3)
        engine.start()
        self.assertEqual(engine.on_gate(at(clock, 2)), "start")
        self.assertEqual(engine.on_gate(at(clock, 2.1)), "ignored:lockout")
        self.assertEqual(engine.on_gate(at(clock, 6)), "lap")


class LockoutTests(unittest.TestCase):
    def test_multiple_edges_from_one_passage_give_one_lap(self):
        engine, clock, _ = make(countdown_s=0, min_lap_s=3)
        engine.start()
        self.assertEqual(engine.on_gate(at(clock, 10)), "lap")
        self.assertEqual(engine.on_gate(at(clock, 10.05)), "ignored:lockout")
        self.assertEqual(engine.on_gate(at(clock, 10.2)), "ignored:lockout")
        # Lockout window is fixed from the accepted passage, not sliding.
        self.assertEqual(engine.on_gate(at(clock, 12.9)), "ignored:lockout")
        self.assertEqual(engine.on_gate(at(clock, 13.1)), "lap")
        snap = engine.snapshot()
        self.assertEqual(snap["lap_count"], 2)
        self.assertEqual(snap["ignored"], {"lockout": 3})


class PauseResumeTests(unittest.TestCase):
    def test_pause_freezes_time_and_ignores_gate(self):
        engine, clock, _ = make(countdown_s=0, min_lap_s=3)
        engine.start()
        engine.on_gate(at(clock, 10))
        clock.set(1000 + 15)
        engine.pause()
        self.assertEqual(engine.state, PAUSED)

        clock.set(1000 + 20)
        self.assertEqual(engine.on_gate(at(clock, 20)), "ignored:paused")
        self.assertEqual(engine.snapshot()["elapsed_ms"], 15000)

        clock.set(1000 + 25)
        engine.resume()
        self.assertEqual(engine.snapshot()["elapsed_ms"], 15000)

        # Race time at t=30 is 15 + 5 = 20 s -> lap 2 = 10 s.
        self.assertEqual(engine.on_gate(at(clock, 30)), "lap")
        lap = engine.snapshot()["laps"][1]
        self.assertEqual((lap["lap_ms"], lap["total_ms"]), (10000, 20000))

    def test_passage_before_pause_delivered_late_counts(self):
        engine, clock, _ = make(countdown_s=0, min_lap_s=1)
        engine.start()
        clock.set(1000 + 10)
        engine.pause()
        self.assertEqual(engine.on_gate(at(clock, 9.99)), "lap")
        self.assertEqual(engine.snapshot()["laps"][0]["total_ms"], 9990)

    def test_passage_spanning_resume_is_not_a_lap(self):
        engine, clock, _ = make(countdown_s=0, min_lap_s=3)
        engine.start()
        clock.set(1000 + 5)
        engine.pause()
        self.assertEqual(engine.on_gate(at(clock, 9.95)), "ignored:paused")
        clock.set(1000 + 10)
        engine.resume()
        self.assertEqual(engine.on_gate(at(clock, 10.05)), "ignored:lockout")

    def test_invalid_pause_resume(self):
        engine, clock, _ = make(countdown_s=5)
        with self.assertRaises(CommandError):
            engine.pause()
        engine.start()
        with self.assertRaises(CommandError):
            engine.pause()  # still counting down
        with self.assertRaises(CommandError):
            engine.resume()


class TimeLimitTests(unittest.TestCase):
    def test_finishes_at_exact_deadline_after_grace(self):
        engine, clock, store = make(countdown_s=0, max_duration_s=30, min_lap_s=1)
        engine.start()
        engine.tick(at(clock, 30))  # deadline reached, still in grace period
        self.assertEqual(engine.state, RUNNING)
        clock.set(1000 + 30.05)
        self.assertEqual(engine.snapshot()["elapsed_ms"], 30000)  # clamped

        # Passage just before the deadline, delivered during the grace period.
        self.assertEqual(engine.on_gate(at(clock, 29.99)), "lap")

        clock.set(1000 + 30 + FINALIZE_GRACE_NS / S + 0.01)
        engine.tick()
        self.assertEqual(engine.state, FINISHED)
        snap = engine.snapshot()
        self.assertEqual(snap["elapsed_ms"], 30000)
        self.assertEqual(snap["finish_reason"], "time_limit")
        self.assertEqual(snap["lap_count"], 1)
        self.assertEqual(len(store.list_recent()), 1)
        self.assertEqual(store.list_recent()[0]["total_ms"], 30000)

    def test_gate_event_at_or_after_deadline_finishes_and_is_ignored(self):
        engine, clock, store = make(countdown_s=0, max_duration_s=30)
        engine.start()
        self.assertEqual(engine.on_gate(at(clock, 30.0)), "ignored:finished")
        self.assertEqual(engine.state, FINISHED)
        self.assertEqual(engine.snapshot()["elapsed_ms"], 30000)
        self.assertEqual(engine.snapshot()["lap_count"], 0)
        self.assertEqual(len(store.list_recent()), 1)

    def test_pause_extends_deadline(self):
        engine, clock, _ = make(countdown_s=0, max_duration_s=30)
        engine.start()
        clock.set(1000 + 10)
        engine.pause()
        clock.set(1000 + 20)
        engine.resume()
        engine.tick(at(clock, 39))
        self.assertEqual(engine.state, RUNNING)
        engine.tick(at(clock, 40) + FINALIZE_GRACE_NS)
        self.assertEqual(engine.state, FINISHED)
        self.assertEqual(engine.snapshot()["elapsed_ms"], 30000)

    def test_gate_mode_limit_counts_from_first_passage(self):
        engine, clock, _ = make(start_mode="gate", countdown_s=0, max_duration_s=10)
        engine.start()
        engine.tick(at(clock, 100))
        self.assertEqual(engine.state, ARMED)  # no limit while armed
        engine.on_gate(at(clock, 100))
        engine.tick(at(clock, 110) + FINALIZE_GRACE_NS)
        self.assertEqual(engine.state, FINISHED)
        self.assertEqual(engine.snapshot()["elapsed_ms"], 10000)

    def test_pause_after_deadline_finishes_instead(self):
        engine, clock, _ = make(countdown_s=0, max_duration_s=30)
        engine.start()
        clock.set(1000 + 30.1)
        with self.assertRaises(CommandError):
            engine.pause()
        self.assertEqual(engine.state, FINISHED)
        self.assertEqual(engine.snapshot()["elapsed_ms"], 30000)

    def test_next_deadline_wait_does_not_block_when_due(self):
        engine, clock, _ = make(countdown_s=5)
        engine.start()
        clock.set(1000 + 6)
        engine.wait_for_next_deadline(10)  # must return immediately
        engine.tick()
        self.assertEqual(engine.state, RUNNING)


class StopResetTests(unittest.TestCase):
    def test_stop_during_countdown_returns_to_idle(self):
        engine, _, store = make(countdown_s=5)
        engine.start()
        engine.stop()
        self.assertEqual(engine.state, IDLE)
        self.assertEqual(store.list_recent(), [])

    def test_stop_while_armed_returns_to_idle(self):
        engine, clock, _ = make(start_mode="gate", countdown_s=0)
        engine.start()
        self.assertEqual(engine.state, ARMED)
        engine.stop()
        self.assertEqual(engine.state, IDLE)

    def test_stop_while_running(self):
        engine, clock, store = make(countdown_s=0)
        engine.start()
        engine.on_gate(at(clock, 10))
        clock.set(1000 + 12.5)
        engine.stop()
        snap = engine.snapshot()
        self.assertEqual(snap["state"], FINISHED)
        self.assertEqual(snap["finish_reason"], "stopped")
        self.assertEqual(snap["elapsed_ms"], 12500)
        self.assertEqual(store.list_recent()[0]["laps"][0]["lap_ms"], 10000)
        # Gate events after the finish are ignored.
        self.assertEqual(engine.on_gate(at(clock, 20)), "ignored:finished")

    def test_stop_while_paused_uses_paused_time(self):
        engine, clock, _ = make(countdown_s=0)
        engine.start()
        clock.set(1000 + 8)
        engine.pause()
        clock.set(1000 + 50)
        engine.stop()
        self.assertEqual(engine.snapshot()["elapsed_ms"], 8000)

    def test_stop_without_race_raises(self):
        engine, _, _ = make()
        with self.assertRaises(CommandError):
            engine.stop()

    def test_reset(self):
        engine, clock, _ = make(countdown_s=0)
        engine.start()
        engine.on_gate(at(clock, 10))
        with self.assertRaises(CommandError):
            engine.reset()
        engine.stop()
        engine.reset()
        snap = engine.snapshot()
        self.assertEqual(snap["state"], IDLE)
        self.assertEqual(snap["laps"], [])
        self.assertEqual(snap["elapsed_ms"], 0)

    def test_start_after_finish_begins_clean_race(self):
        engine, clock, _ = make(countdown_s=0)
        engine.start()
        engine.on_gate(at(clock, 10))
        engine.stop()
        first_id = engine.snapshot()["race_id"]
        clock.set(1000 + 60)
        engine.start()
        snap = engine.snapshot()
        self.assertEqual(snap["state"], RUNNING)
        self.assertEqual(snap["laps"], [])
        self.assertEqual(snap["race_id"], first_id + 1)

    def test_start_twice_raises(self):
        engine, _, _ = make()
        engine.start()
        with self.assertRaises(CommandError):
            engine.start()

    def test_gate_when_idle_ignored(self):
        engine, clock, _ = make()
        self.assertEqual(engine.on_gate(at(clock, 1)), "ignored:idle")
        self.assertEqual(engine.state, IDLE)


class SettingsTests(unittest.TestCase):
    def test_update_and_validate(self):
        engine, _, _ = make()
        engine.update_settings({"start_mode": "gate", "countdown_s": 10, "max_duration_s": 300})
        settings = engine.snapshot()["settings"]
        self.assertEqual(settings["start_mode"], "gate")
        self.assertEqual(settings["max_duration_s"], 300.0)
        for bad in (
            {"start_mode": "fast"},
            {"countdown_s": -1},
            {"countdown_s": "5"},
            {"min_lap_s": 0},
            {"max_duration_s": float("nan")},
            {"unknown": 1},
            {"countdown_s": True},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                engine.update_settings(bad)

    def test_locked_during_race(self):
        engine, _, _ = make()
        engine.start()
        with self.assertRaises(CommandError):
            engine.update_settings({"countdown_s": 3})


class NotificationTests(unittest.TestCase):
    def test_version_changes_on_every_mutation(self):
        engine, clock, _ = make(countdown_s=0)
        v0 = engine.wait_for_change(None, 0)
        engine.start()
        v1 = engine.wait_for_change(v0, 0)
        self.assertGreater(v1, v0)
        engine.on_gate(at(clock, 5))
        self.assertGreater(engine.wait_for_change(v1, 0), v1)

    def test_wait_times_out_without_change(self):
        engine, _, _ = make()
        v = engine.wait_for_change(None, 0)
        self.assertEqual(engine.wait_for_change(v, 0.01), v)


if __name__ == "__main__":
    unittest.main()
