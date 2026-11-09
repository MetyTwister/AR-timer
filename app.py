#!/usr/bin/env python3
"""RC lap timer: web UI, JSON API and live updates for a light-curtain gate.

Architecture (stdlib only, apart from gpiozero on the Pi):
  * GateSensor (gate.py)   - GPIO edge callbacks, timestamps beam breaks.
  * RaceEngine (race.py)   - authoritative race state and timing.
  * scheduler thread       - applies countdown end / time limit on schedule.
  * ThreadingHTTPServer    - static UI, POST commands, Server-Sent Events.
"""

from __future__ import annotations

import json
import logging
import signal
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from gate import GateSensor
from race import CommandError, RaceEngine
from store import MemoryRaceStore
from sysinfo import SystemInfo

HOST = "0.0.0.0"
PORT = 8000
ROOT = Path(__file__).resolve().parent
PIN = 4  # BCM 4 = physical header pin 7
STUCK_BEAM_S = 2.0  # report a sensor fault if the beam stays broken this long
HEARTBEAT_S = 1.0  # SSE keep-alive / clock resync interval
SCHEDULER_MAX_WAIT_S = 0.5
MAX_BODY_BYTES = 4096

# Only these files are served; everything else in the project directory
# (source, tests, .git) stays private.
STATIC_FILES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/app.js": "app.js",
    "/styles.css": "styles.css",
}

log = logging.getLogger("lap-timer")


class AppContext:
    def __init__(self, engine: RaceEngine, gate: GateSensor, store, sysinfo: SystemInfo):
        self.engine = engine
        self.gate = gate
        self.store = store
        self.sysinfo = sysinfo
        self.shutdown = threading.Event()


class RequestHandler(SimpleHTTPRequestHandler):
    ctx: AppContext  # set in main()
    timeout = 15  # socket timeout: a vanished phone cannot block a thread forever

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    # ----------------------------------------------------------------- routing

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        route = {
            "/api/events": self.stream_events,
            "/api/state": self.send_state,
            "/api/status": self.send_sensor_status,
            "/api/system": self.send_system,
            "/api/history": self.send_history,
        }.get(path)
        if route:
            route()
        elif path in STATIC_FILES:
            self.path = "/" + STATIC_FILES[path]
            super().do_GET()
        else:
            self.send_json({"ok": False, "error": "Not found"}, status=404)

    def do_HEAD(self):
        path = self.path.split("?", 1)[0]
        if path in STATIC_FILES:
            self.path = "/" + STATIC_FILES[path]
            super().do_HEAD()
        else:
            self.send_error(404)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        engine = self.ctx.engine
        commands = {
            "/api/race/start": engine.start,
            "/api/race/pause": engine.pause,
            "/api/race/resume": engine.resume,
            "/api/race/stop": engine.stop,
            "/api/race/reset": engine.reset,
        }
        try:
            if path in commands:
                commands[path]()
            elif path == "/api/settings":
                engine.update_settings(self.read_json_body())
            else:
                self.send_json({"ok": False, "error": "Not found"}, status=404)
                return
        except CommandError as exc:
            self.send_json({"ok": False, "error": str(exc), "race": engine.snapshot()}, status=409)
            return
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=400)
            return
        self.send_json({"ok": True, "race": engine.snapshot()})

    # --------------------------------------------------------------- endpoints

    def send_state(self):
        self.send_json({"ok": True, "race": self.ctx.engine.snapshot(),
                        "sensor": self.ctx.gate.status()})

    def send_sensor_status(self):
        sensor = self.ctx.gate.status()
        self.send_json({"ok": sensor["available"], **sensor},
                       status=200 if sensor["available"] else 503)

    def send_system(self):
        self.send_json({
            "ok": True,
            "system": self.ctx.sysinfo.get(),
            "sensor": self.ctx.gate.status(),
            "race_state": self.ctx.engine.state,
        })

    def send_history(self):
        self.send_json({"ok": True, "races": self.ctx.store.list_recent()})

    def stream_events(self):
        """Server-Sent Events: full state on every change, light heartbeat otherwise.

        Snapshots are taken via the engine's lock but written to the socket
        outside it, so a slow client never delays GPIO or timer processing.
        """
        engine, gate = self.ctx.engine, self.ctx.gate
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        version = None
        try:
            self.wfile.write(b"retry: 2000\n\n")
            while not self.ctx.shutdown.is_set():
                current = engine.wait_for_change(version, HEARTBEAT_S)
                if current != version:
                    race = engine.snapshot()
                    version = race["version"]
                    self.send_event("state", {"race": race, "sensor": gate.status()})
                else:
                    self.send_event("status", {"race": engine.snapshot(include_laps=False),
                                               "sensor": gate.status()})
        except OSError:  # client went away (broken pipe, reset, timeout)
            return

    # ----------------------------------------------------------------- helpers

    def send_event(self, event: str, payload: dict):
        data = json.dumps(payload, separators=(",", ":"))
        self.wfile.write(f"event: {event}\ndata: {data}\n\n".encode("utf-8"))
        self.wfile.flush()

    def read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("Invalid Content-Length")
        if length > MAX_BODY_BYTES:
            raise ValueError("Request body too large")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            raise ValueError("Request body must be valid JSON")

    def send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, format, *args):
        # Keep the journal quiet: log only errors, not every API poll.
        if len(args) >= 2 and str(args[1]).startswith(("2", "3")):
            return
        super().log_message(format, *args)


def run_scheduler(engine: RaceEngine, stop: threading.Event) -> None:
    """Apply countdown end and time limit at their scheduled moments."""
    while not stop.is_set():
        try:
            engine.tick()
            engine.wait_for_next_deadline(SCHEDULER_MAX_WAIT_S)
        except Exception:
            log.exception("Scheduler error")
            stop.wait(SCHEDULER_MAX_WAIT_S)


def _raise_keyboard_interrupt(signum, frame):
    raise KeyboardInterrupt


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = MemoryRaceStore()
    engine = RaceEngine(store=store)
    gate = GateSensor(PIN, engine.on_gate, stuck_after_s=STUCK_BEAM_S)
    ctx = AppContext(engine, gate, store, SystemInfo())
    RequestHandler.ctx = ctx

    scheduler = threading.Thread(target=run_scheduler, args=(engine, ctx.shutdown),
                                 name="race-scheduler", daemon=True)
    scheduler.start()

    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)  # clean stop under systemd
    server = ThreadingHTTPServer((HOST, PORT), RequestHandler)
    server.daemon_threads = True
    log.info("RC lap timer running at http://%s:%d (gate on BCM %d%s)", HOST, PORT, PIN,
             "" if gate.available else f" – UNAVAILABLE: {gate.error}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Stopping RC lap timer")
    finally:
        ctx.shutdown.set()
        server.server_close()
        gate.close()


if __name__ == "__main__":
    main()
