"""Measure backend startup: process launch → first ``GET /health`` 200, and → embedding model warm.

    uv run python scripts/bench_startup.py                         # Neo4j unreachable → in-memory
    uv run python scripts/bench_startup.py --neo4j bolt://localhost:7688 --neo4j-password bench-pass
    uv run python scripts/bench_startup.py --embeddings qwen --runs 3

Runs the real ``backend.daemon.server:main`` on ``--port`` (default 8775, never the user's 8765)
with a scratch data dir, a throwaway data key, the mock LLM and the file watcher off. Point
``--neo4j`` only at a scratch container: startup encrypts a plaintext graph in place (:7687 is refused).
"""
from __future__ import annotations

import argparse
import base64
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def one_run(args: argparse.Namespace) -> tuple[float | None, float | None, list[str]]:
    tmp = tempfile.mkdtemp(prefix="polytone-startup-")
    env = {
        **os.environ,
        "POLYTONE_API_PORT": str(args.port),
        "POLYTONE_DATA_DIR": tmp,
        "POLYTONE_WATCH_DIR": str(Path(tmp) / "inbox"),
        "POLYTONE_WATCHER": "0",
        "POLYTONE_LLM": "mock",
        "POLYTONE_EMBEDDINGS": args.embeddings,
        "NEO4J_URI": args.neo4j,
        "NEO4J_PASSWORD": args.neo4j_password,
        "PYTHONUNBUFFERED": "1",
        # throwaway data key: never read / create the real Keychain item from a benchmark
        "POLYTONE_DATA_KEY": base64.b64encode(os.urandom(32)).decode(),
        "POLYTONE_API_TOKEN": base64.urlsafe_b64encode(os.urandom(32)).decode(),
        "WISPR_HISTORY_DIRS": "/nonexistent",
        "POLYTONE_WISPR_LIVE": "0",
    }
    env.pop("POLYTONE_PARENT_PID", None)
    t0 = time.perf_counter()
    proc = subprocess.Popen(
        [sys.executable, "-c", "from backend.daemon.server import main; main()"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    lines: list[str] = []
    warm: list[float] = []

    def reader() -> None:
        assert proc.stdout is not None
        for ln in proc.stdout:
            lines.append(ln.rstrip())
            if "embedding model warm" in ln or "embedding model unavailable" in ln:
                warm.append(time.perf_counter() - t0)

    threading.Thread(target=reader, daemon=True).start()
    health = None
    deadline = t0 + args.timeout
    while time.perf_counter() < deadline and proc.poll() is None:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{args.port}/health", timeout=0.5) as r:
                if r.status == 200:
                    health = time.perf_counter() - t0
                    break
        except OSError:
            time.sleep(0.02)
    if args.embeddings != "none":
        while not warm and time.perf_counter() < deadline and proc.poll() is None:
            time.sleep(0.05)
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    return health, (warm[0] if warm else None), lines


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8775)
    ap.add_argument("--neo4j", default="bolt://localhost:7699", help="default: an unused port (unreachable)")
    ap.add_argument("--neo4j-password", default="bench-pass")
    ap.add_argument("--embeddings", default="none", choices=["none", "qwen"])
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=90)
    ap.add_argument("--show-log", action="store_true")
    args = ap.parse_args()
    if args.neo4j.rstrip("/").endswith(":7687"):
        sys.exit("refusing :7687 — the backend migrates the graph at start; that is the user's real style graph")
    if args.port == 8765:
        sys.exit("refusing to use 8765 — the user's backend may be listening there")
    hs, ws = [], []
    for i in range(args.runs):
        h, w, lines = one_run(args)
        print(f"run {i + 1}: /health {h if h is None else f'{h:.2f}s'}  embeddings warm "
              f"{w if w is None else f'{w:.2f}s'}")
        if args.show_log or h is None:
            print("\n".join("    " + ln for ln in lines[-40:]))
        if h is not None:
            hs.append(h)
        if w is not None:
            ws.append(w)
    if hs:
        print(f"/health median {statistics.median(hs):.2f}s (min {min(hs):.2f}s)")
    if ws:
        print(f"embeddings warm median {statistics.median(ws):.2f}s")


if __name__ == "__main__":
    main()
