#!/usr/bin/env python3
"""The inspector: run every check CI runs, locally, and save what failed.

    python3 validate.py              # Python + frontend always; Rust only if src-tauri/ changed vs main
    python3 validate.py --all        # force the Rust checks too
    python3 validate.py --only python,rust

Mirrors .github/workflows/ci.yml:
    python    uv run ruff check backend · uv run pytest -q
    frontend  pnpm build (tsc + vite)
    rust      cargo test --locked · cargo clippy --all-targets --locked -- -D warnings

Writes (both git-ignored):
    .agent/validation.json   every check: ok, seconds, command, output tail
    .agent/feedback.log      only the failures, stderr first: what orchestrator.py hands back to the agent

Exit code 0 = everything passed, 1 = something failed. Standard library only.
Tests run offline exactly like CI: ANTHROPIC_API_KEY is blanked (mock LLM) and conftest.py
supplies a test data key, so nothing touches the real Keychain, data/ or the real Neo4j.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / ".agent"
TAIL_LINES = 150  # per failing check; enough for a traceback, small enough for a prompt
TIMEOUT_S = 900


@dataclass
class Check:
    name: str
    lang: str
    cmd: list[str]
    cwd: str = "."


CHECKS = [
    Check("ruff", "python", ["uv", "run", "ruff", "check", "backend", "validate.py", "orchestrator.py"]),
    Check("pytest", "python", ["uv", "run", "pytest", "-q"]),
    Check("pnpm build", "frontend", ["pnpm", "build"]),  # also produces dist/, which the Rust build needs
    Check("cargo test", "rust", ["cargo", "test", "--locked"], "src-tauri"),
    Check("cargo clippy", "rust", ["cargo", "clippy", "--all-targets", "--locked", "--", "-D", "warnings"], "src-tauri"),
]
LANGS = ("python", "frontend", "rust")


def _git(*args: str) -> str:
    r = subprocess.run(["git", *args], check=False, cwd=ROOT, capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def rust_changed() -> bool:
    """Same rule as CI: Rust checks only when src-tauri/ differs from main (committed or not)."""
    base = _git("merge-base", "HEAD", "origin/main") or _git("merge-base", "HEAD", "main")
    if not base:
        return True  # can't tell: be safe
    committed = _git("diff", "--name-only", base, "HEAD", "--", "src-tauri")
    uncommitted = _git("status", "--porcelain", "--", "src-tauri")
    return bool(committed or uncommitted)


def tail(text: str, n: int = TAIL_LINES) -> str:
    lines = text.rstrip().splitlines()
    cut = len(lines) - n
    return ("\n".join([f"... ({cut} earlier lines cut)"] + lines[-n:])) if cut > 0 else "\n".join(lines)


def run(check: Check) -> dict:
    env = {**os.environ, "ANTHROPIC_API_KEY": "", "CI": "1"}  # offline, like CI; never the real key
    start = time.monotonic()
    try:
        r = subprocess.run(check.cmd, check=False, cwd=ROOT / check.cwd, env=env, capture_output=True, text=True,
                           timeout=TIMEOUT_S)
        code, out, err = r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        code, out, err = 127, "", f"{check.cmd[0]}: command not found (install it or fix PATH)"
    except subprocess.TimeoutExpired as e:
        code = 124
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = f"timed out after {TIMEOUT_S}s"
    return {
        "name": check.name, "lang": check.lang, "ok": code == 0, "exit_code": code,
        "seconds": round(time.monotonic() - start, 1),
        "command": " ".join(check.cmd) + (f"   (in {check.cwd}/)" if check.cwd != "." else ""),
        # stderr first: compiler and tool errors land there; pytest and ruff report on stdout
        "stderr_tail": tail(err), "stdout_tail": tail(out),
    }


def feedback(results: list[dict]) -> str:
    parts = []
    for r in results:
        if r["ok"]:
            continue
        parts.append(f"=== FAILED [{r['lang']}] {r['name']}  (exit {r['exit_code']})\n$ {r['command']}")
        if r["stderr_tail"]:
            parts.append(f"--- stderr\n{r['stderr_tail']}")
        if r["stdout_tail"]:
            parts.append(f"--- stdout\n{r['stdout_tail']}")
    return "\n".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--all", action="store_true", help="run the Rust checks even if src-tauri/ is unchanged")
    ap.add_argument("--only", help=f"comma-separated subset of {','.join(LANGS)}")
    a = ap.parse_args()

    langs = set(a.only.split(",")) if a.only else set(LANGS)
    if unknown := langs - set(LANGS):
        ap.error(f"unknown: {','.join(sorted(unknown))}")
    if not a.only and not a.all and not rust_changed():
        langs.discard("rust")

    results = []
    for check in CHECKS:
        if check.lang not in langs:
            continue
        print(f"→ {check.name} …", end=" ", flush=True)
        r = run(check)
        print(("ok" if r["ok"] else "FAILED") + f" ({r['seconds']}s)", flush=True)
        results.append(r)

    passed = all(r["ok"] for r in results)
    OUT.mkdir(exist_ok=True)
    report = {"passed": passed, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "skipped": sorted(set(LANGS) - langs),
              "checks": results}
    (OUT / "validation.json").write_text(json.dumps(report, indent=2))
    (OUT / "feedback.log").write_text(feedback(results))
    if report["skipped"]:
        print(f"skipped: {', '.join(report['skipped'])} (unchanged; --all to force)")
    print("PASSED" if passed else f"FAILED, details in {OUT / 'feedback.log'}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
