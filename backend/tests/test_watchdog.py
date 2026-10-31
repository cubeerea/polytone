import os
import subprocess
import sys
import time

from backend.daemon.watchdog import parent_alive, start_parent_watchdog


def test_no_parent_pid_means_no_watchdog(monkeypatch):
    monkeypatch.delenv("POLYTONE_PARENT_PID", raising=False)
    assert start_parent_watchdog() is None


def test_parent_alive():
    assert parent_alive(os.getpid())
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    assert not parent_alive(p.pid)


def test_child_exits_when_parent_dies():
    # "app" spawns a "backend" running the watchdog, then waits on stdin; closing stdin kills the app
    code = (
        "import os, subprocess, sys;"
        "env = dict(os.environ, POLYTONE_PARENT_PID=str(os.getpid()));"
        "c = subprocess.Popen([sys.executable, '-c', "
        "'from backend.daemon.watchdog import start_parent_watchdog as s; import time; "
        "assert s(interval=0.1) is not None; time.sleep(30)'], env=env);"
        "print(c.pid, flush=True); sys.stdin.read()"
    )
    app = subprocess.Popen(
        [sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    child = int(app.stdout.readline())
    time.sleep(0.5)
    assert parent_alive(child), "backend should run while the app is alive"
    app.stdin.close()
    app.wait(timeout=5)
    deadline = time.time() + 10
    while time.time() < deadline and parent_alive(child):
        time.sleep(0.1)
    assert not parent_alive(child)
