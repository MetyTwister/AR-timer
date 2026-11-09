# RC Lap Timer

A lap timer for RC aircraft. It runs on a Raspberry Pi Zero, with a light-curtain timing gate on a GPIO input. All timing happens on the Pi. Any phone or laptop on the same network can control the race and see laps live in a browser.

## Features

- **Countdown, then one of two start modes:**
  - **Automatic:** the race timer starts the moment the countdown reaches zero.
  - **Gate:** the timer is *armed* after the countdown and starts on the first passage through the gate. That passage starts the clock and does not count as a lap.
- Every later passage records a lap: lap number, lap time and total race time.
- **Pause** freezes the race clock, and gate passages are ignored while paused. **Resume** continues from the frozen time. **Stop** ends the race. **Clear laps** resets everything (tap twice to confirm).
- A configurable **maximum race duration** stops the race automatically, at exactly the limit.
- A **minimum lap time (lockout)** stops one passage from counting as several laps. Fuselage, wings and tail can each break the beam.
- Live updates over Server-Sent Events, with no page refresh.
- Sensor and system panel: beam state, GPIO level, trigger count, ignored triggers, IP address, Wi-Fi, CPU temperature, load and uptime. A warning appears if the beam stays blocked.

## Files

| File | Purpose |
|---|---|
| `app.py` | HTTP server, JSON API, SSE stream, scheduler thread, wiring |
| `race.py` | Race state machine and timing (pure logic, no GPIO) |
| `gate.py` | GPIO edge handling for the light curtain (gpiozero) |
| `store.py` | Race history interface (in memory for now; SQLite can be added later) |
| `sysinfo.py` | Pi diagnostics for the system panel |
| `index.html`, `app.js`, `styles.css` | Web UI (no build step, no external resources) |
| `tests/test_race.py` | Unit tests for the race logic |

## Wiring and safety

- Gate input: **BCM GPIO 4 (physical header pin 7)**, internal pull-up enabled.
- The light curtain must pull the pin to **GND (LOW) while the beam is broken**. With the beam clear, the line is HIGH. An NPN / open-collector output, or a relay contact to GND, fits this directly.
- Raspberry Pi GPIO pins are **3.3 V only**. Many light curtains run on 12–24 V. Never connect a powered (PNP / push-pull) 12–24 V output directly. Use an open-collector output, an optocoupler or a level shifter.
- For long sensor cables, an optocoupler or a small RC filter (e.g. 1 kΩ in series + 100 nF to GND at the Pi) reduces false triggers from electrical noise.

## Run on Raspberry Pi OS

```sh
sudo apt update
sudo apt install python3-gpiozero python3-lgpio
cd /path/to/AR-timer
python3 app.py
```

Open `http://<raspberry-pi-address>:8000` from a phone or laptop on the same network. The address is also shown in the system panel. Stop the server with Ctrl+C (SIGTERM also works, e.g. under systemd).

If the gate sensor shows *unavailable*, check that `python3-gpiozero` is installed and that the user is in the `gpio` group:

```sh
sudo usermod -aG gpio "$USER"   # then log out and back in
```

The web UI still works without GPIO (for example on a laptop), but no gate events arrive.

Configuration constants (`PORT`, `PIN`, `STUCK_BEAM_S`) are at the top of `app.py`. Race settings (start mode, countdown, max duration, minimum lap time) are changed in the UI. They are kept in memory and reset to defaults when the server restarts.

## Timing design

- The beam-broken edge is timestamped with `time.monotonic_ns()` on the Pi, in the GPIO callback, before any other work. That timestamp is what gets measured, not when the event is processed and not the browser's clock. The monotonic clock is not affected by NTP adjusting the Pi's (RTC-less) wall clock.
- Expected timing uncertainty: a few milliseconds of Python callback latency on a Pi Zero.
- The countdown end and the time limit are recorded at their scheduled times. A passage timestamped just before the time limit is still counted if it is delivered up to 150 ms late. Passages at or after the limit are rejected.
- Lap time = race time between passages, excluding paused time.
- Lockout: after an accepted passage, triggers are ignored for the minimum lap time (default 3 s). A trigger ignored during the countdown or a pause also starts a lockout window. A passage that spans the start or a resume therefore cannot create a bogus short lap.
- The browser only interpolates the displayed clock between server updates (re-synced every second). Lap data always comes from the Pi.

## API

| Method | Path | Description |
|---|---|---|
| GET | `/api/events` | SSE stream: `state` (full snapshot on every change), `status` (1 s heartbeat) |
| GET | `/api/state` | Current race snapshot + sensor status |
| GET | `/api/system` | System diagnostics |
| GET | `/api/status` | Sensor status only |
| GET | `/api/history` | Finished races since the server started |
| POST | `/api/race/start` · `pause` · `resume` · `stop` · `reset` | Race commands (409 if not allowed in the current state) |
| POST | `/api/settings` | JSON body with any of `start_mode` (`auto`/`gate`), `countdown_s`, `max_duration_s` (0 = none), `min_lap_s` |

## Tests

The race logic tests use a fake clock and need no Pi:

```sh
python3 -m unittest discover -s tests -v
```

## On-Pi test checklist

1. Open the UI on two devices at once. Both should update live.
2. In READY state, wave a hand through the beam. *Beam* shows BROKEN/clear, *Triggers* counts up, and no laps are recorded.
3. Automatic mode, 5 s countdown: the clock starts from 0 when the countdown ends, and passages create laps.
4. Gate mode: the state shows ARMED after the countdown. The first passage starts the clock without a lap, and the second records lap 1.
5. Break the beam several times quickly (under the minimum lap time). Only one lap is recorded, and *Ignored* shows `lockout`.
6. Pause: the clock freezes and passages show as `paused` under *Ignored*. Resume: the clock continues, and the next lap time excludes the paused period.
7. Max duration 30 s: the race finishes at exactly `0:30.000`.
8. Block the beam for more than 2 s: a sensor warning appears.
9. Unplug Wi-Fi or close the server: the UI shows *Disconnected* and reconnects by itself.
10. Run `top` during a race: CPU use of `python3` should stay low.
