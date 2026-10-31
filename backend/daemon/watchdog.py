"""Exit the backend when the app that spawned it is gone.

The Tauri app passes ``POLYTONE_PARENT_PID`` when it starts the backend. If the app is
force-quit or crashes, its exit hooks never run, and an orphaned backend would keep serving
old code on the port the next launch reuses. A backend started by hand has no parent pid
and is left alone.
"""
from __future__ import annotations

import logging
import os
import signal
import threading
import time

log = logging.getLogger(__name__)


def parent_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, owned by someone else
        return True
    return True


def start_parent_watchdog(interval: float = 2.0) -> threading.Thread | None:
    raw = os.environ.get("POLYTONE_PARENT_PID", "").strip()
    if not raw.isdigit():
        return None
    pid = int(raw)

    def watch() -> None:
        while parent_alive(pid) and os.getppid() != 1:
            time.sleep(interval)
        log.info("parent app (pid %d) is gone; shutting down", pid)
        os.kill(os.getpid(), signal.SIGTERM)  # uvicorn shuts down cleanly on SIGTERM

    t = threading.Thread(target=watch, name="polytone-parent-watchdog", daemon=True)
    t.start()
    return t
