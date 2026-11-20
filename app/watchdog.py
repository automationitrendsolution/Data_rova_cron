"""Unfreeze a run whose browser stopped responding.

Some Playwright calls (reading the page with ``evaluate``) have no time
limit.  When Chromium's page process dies -- seen on 2026-11-19 after a very
large 90-day rank table -- such a call waits forever and the run hangs
without any error.

The watchdog notes every sign of progress (every log line, plus explicit
``beat()`` calls from long waits such as waiting for exports).  If nothing
happens for ``limit_minutes``, it kills this run's Chromium processes: the
stuck call then fails with an ordinary error, and the run's retry starts a
fresh browser and carries on.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

from .logging_config import LOGGER_NAME, get_logger

logger = get_logger("watchdog")

_last_beat = time.monotonic()
_lock = threading.Lock()


def beat() -> None:
    """Record that the run is making progress."""
    global _last_beat
    with _lock:
        _last_beat = time.monotonic()


def _idle_seconds() -> float:
    with _lock:
        return time.monotonic() - _last_beat


class _BeatOnLog(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401 - logging API
        beat()


def kill_browser(session_dir: Path) -> int:
    """Kill the Chromium processes started on ``session_dir``; returns how many."""
    marker = str(session_dir)
    if sys.platform == "win32":
        script = (
            "$m = '" + marker.replace("'", "''") + "'; $n = 0; "
            "Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'chrome' -and $_.CommandLine -and "
            "$_.CommandLine.Contains($m) } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
            "-ErrorAction SilentlyContinue; $n++ }; $n"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True, timeout=120
        )
        try:
            return int((result.stdout or "0").strip().splitlines()[-1])
        except (ValueError, IndexError):
            return 0
    result = subprocess.run(["pkill", "-f", marker], capture_output=True, timeout=60)
    return 1 if result.returncode == 0 else 0


class Watchdog:
    """Background thread; use as a context manager around the run."""

    def __init__(self, session_dir: Path, *, limit_minutes: float) -> None:
        self.session_dir = session_dir
        self.limit_seconds = limit_minutes * 60
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._watch, name="watchdog", daemon=True)
        self._handler = _BeatOnLog()

    def __enter__(self) -> Watchdog:
        beat()
        logging.getLogger(LOGGER_NAME).addHandler(self._handler)
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        logging.getLogger(LOGGER_NAME).removeHandler(self._handler)

    def _watch(self) -> None:
        while not self._stop.wait(30):
            idle = _idle_seconds()
            if idle < self.limit_seconds:
                continue
            logger.warning(
                "No progress for %d min: the browser has stopped responding. Killing it so the "
                "run can retry with a fresh one.",
                idle // 60,
            )
            try:
                killed = kill_browser(self.session_dir)
                logger.warning("Watchdog stopped %d browser process(es)", killed)
            except Exception as exc:  # noqa: BLE001 - never let the watchdog crash the run
                logger.warning("Watchdog could not stop the browser: %s", exc)
            beat()
