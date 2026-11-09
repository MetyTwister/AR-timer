"""Lightweight Raspberry Pi diagnostics for the system information panel.

Values are collected on demand (only when a client asks) and cached briefly,
so the occasional subprocess call never runs more than once every few seconds.
Every probe is optional: on a non-Pi machine missing values are simply None.
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import threading
import time
from typing import Optional

CACHE_S = 10.0


def _read(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as fh:
            return fh.read().decode("utf-8", "replace").strip("\x00\n ")
    except OSError:
        return None


def _run(*cmd: str) -> Optional[str]:
    if shutil.which(cmd[0]) is None:
        return None
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=2, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _ip_addresses() -> list[str]:
    out = _run("hostname", "-I")
    if out:
        return out.split()
    try:
        _, _, addrs = socket.gethostbyname_ex(socket.gethostname())
        return [a for a in addrs if not a.startswith("127.")]
    except OSError:
        return []


def _wifi_ssid() -> Optional[str]:
    ssid = _run("iwgetid", "-r")
    if ssid:
        return ssid
    # NetworkManager (Raspberry Pi OS Bookworm): active Wi-Fi connection name.
    out = _run("nmcli", "-t", "-f", "NAME,TYPE", "connection", "show", "--active")
    for line in (out or "").splitlines():
        name, _, kind = line.rpartition(":")
        if kind == "802-11-wireless":
            return name.replace("\\:", ":")
    return None


def _cpu_temp_c() -> Optional[float]:
    raw = _read("/sys/class/thermal/thermal_zone0/temp")
    try:
        return round(int(raw) / 1000, 1) if raw else None
    except ValueError:
        return None


def _uptime_s() -> Optional[int]:
    raw = _read("/proc/uptime")
    try:
        return int(float(raw.split()[0])) if raw else None
    except (ValueError, IndexError):
        return None


class SystemInfo:
    def __init__(self):
        self._lock = threading.Lock()
        self._cached: Optional[dict] = None
        self._cached_at = 0.0
        self._started = time.monotonic()

    def get(self) -> dict:
        with self._lock:
            now = time.monotonic()
            if self._cached is None or now - self._cached_at > CACHE_S:
                self._cached = self._collect()
                self._cached_at = now
            info = dict(self._cached)
        info["server_uptime_s"] = int(time.monotonic() - self._started)
        return info

    @staticmethod
    def _collect() -> dict:
        try:
            load = [round(x, 2) for x in os.getloadavg()]
        except OSError:
            load = None
        return {
            "hostname": socket.gethostname(),
            "ip_addresses": _ip_addresses(),
            "wifi_ssid": _wifi_ssid(),
            "model": _read("/proc/device-tree/model") or platform.machine(),
            "cpu_temp_c": _cpu_temp_c(),
            "load_avg": load,
            "uptime_s": _uptime_s(),
            "python": platform.python_version(),
        }
