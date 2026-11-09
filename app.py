#!/usr/bin/env python3
"""Serve the GPIO 17 status page and a small JSON status endpoint."""

from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path

HOST = "0.0.0.0"
PORT = 8000
ROOT = Path(__file__).resolve().parent
PIN = 4

try:
    from gpiozero import Button

    # With a pull-up, an unpressed input reads HIGH; grounding it reads LOW.
    gpio_input = Button(PIN, pull_up=True)
    gpio_error = None
except Exception as exc:  # Keep the page available if GPIO setup fails.
    gpio_input = None
    gpio_error = str(exc)


class RequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/api/status":
            self.send_status()
            return
        super().do_GET()

    def send_status(self):
        if gpio_input is None:
            self.send_json(
                {"ok": False, "error": f"GPIO setup failed: {gpio_error}"},
                status=503,
            )
            return

        try:
            high = not gpio_input.is_pressed
            self.send_json(
                {
                    "ok": True,
                    "pin": PIN,
                    "level": "HIGH" if high else "LOW",
                    "high": high,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        except Exception as exc:
            self.send_json({"ok": False, "error": f"GPIO read failed: {exc}"}, status=503)

    def send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), RequestHandler)
    print(f"GPIO 17 monitor running at http://0.0.0.0:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping GPIO monitor")
    finally:
        server.server_close()
        if gpio_input is not None:
            gpio_input.close()
