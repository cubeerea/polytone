#!/usr/bin/env python3
"""The driver: give a headless Claude Code agent one task, validate its work, feed failures back, repeat.

    python3 orchestrator.py new "Add per-app persona rules" --details "..." [--max-iterations 8]
    python3 orchestrator.py run            # work the task until it passes, blocks or hits max iterations
    python3 orchestrator.py run --watch    # always-on: after that, wait for the next `new` task
    python3 orchestrator.py run --pr       # on pass: push the branch and open a PR (never merges)
    python3 orchestrator.py status | stop

Loop, per iteration:
    1. read progress.json → build the execution context (task, iteration, last feedback, git state, rules)
    2. run the agent:  claude -p <context> --output-format json  (permissions: see AGENT_ARGS)
    3. guard: the agent must not touch the inspector, this driver, CI or secrets
    4. run validate.py; failures (stderr first) become `last_feedback` for the next iteration
    5. passed + committed → status "passed"; otherwise loop until max_iterations → "blocked"

State lives in progress.json (git-ignored, created by `new`). Logs: .agent/logs/. To stop between
steps: `python3 orchestrator.py stop` (or create .agent/STOP). Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROGRESS = ROOT / "progress.json"
AGENT_DIR = ROOT / ".agent"
LOGS = AGENT_DIR / "logs"
STOP_FILE = AGENT_DIR / "STOP"

AGENT_TIMEOUT_S = 45 * 60
WATCH_POLL_S = 30
FEEDBACK_MAX_CHARS = 12_000  # keep the injected log from swamping the prompt
MAX_AGENT_ERRORS = 3  # consecutive crashes/timeouts (rate limits, auth) before giving up
BASE = os.environ.get("POLYTONE_AGENT_BASE", "origin/main")  # task branches are cut from this

# Headless agent. acceptEdits + an allowlist: it can edit files and run the checks and local git,
# nothing else runs without a prompt (and -p can't prompt, so it is refused). No push/PR/merge:
# the orchestrator does that only after validation passes.
AGENT_ARGS = [
    "--output-format", "json",
    "--permission-mode", "acceptEdits",
    "--allowedTools",
    "Read", "Edit", "Write", "Glob", "Grep",
    "Bash(python3 validate.py*)",
    "Bash(uv run ruff *)", "Bash(uv run pytest *)", "Bash(uv sync*)",
    "Bash(pnpm build*)", "Bash(pnpm install*)",
    "Bash(cargo test*)", "Bash(cargo clippy*)", "Bash(cargo check*)", "Bash(cargo build*)",
    "Bash(git status*)", "Bash(git diff*)", "Bash(git log*)", "Bash(git show*)",
    "Bash(git add *)", "Bash(git commit *)", "Bash(git restore *)",
    "Bash(ls *)", "Bash(cat *)", "Bash(grep *)", "Bash(rg *)",
    "--disallowedTools",
    "Bash(git push*)", "Bash(gh *)", "Bash(git reset --hard*)", "Bash(git checkout*)", "Bash(git switch*)",
    "Bash(rm -rf*)", "Bash(curl *)", "Bash(security *)",
    "Read(./data/**)", "Read(./.env)", "Read(./.env.*)", "Read(./credentials.json)", "Read(./token.json)",
    "Edit(./validate.py)", "Edit(./orchestrator.py)", "Edit(./progress.json)", "Edit(./.github/**)",
    "Write(./validate.py)", "Write(./orchestrator.py)", "Write(./progress.json)", "Write(./.github/**)",
]

# The agent may not change how it is judged, nor touch secrets. Checked after every agent run.
PROTECTED = ("validate.py", "orchestrator.py", ".github/")
FORBIDDEN = ("data/", ".env", "credentials.json", "token.json")

TEMPLATE = {
    "task": "",
    "details": "",
    "branch": "",
    "status": "idle",  # idle | pending | running | validating | failed | passed | blocked | stopped
    "iteration": 0,
    "max_iterations": 8,
    "budget_usd_per_iteration": 5.0,
    "last_feedback": "",
    "pr_url": "",
    "total_cost_usd": 0.0,
    "history": [],  # one entry per iteration: {iteration, result, seconds, cost_usd, at}
    "updated_at": "",
}


# ---------------------------------------------------------------- state

def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def load() -> dict:
    if not PROGRESS.exists():
        return dict(TEMPLATE)
    return {**TEMPLATE, **json.loads(PROGRESS.read_text())}


def save(p: dict, **changes) -> dict:
    p.update(changes, updated_at=now())
    tmp = PROGRESS.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(p, indent=2) + "\n")
    tmp.replace(PROGRESS)  # atomic: `status` in another pane never reads half a file
    return p


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------- git

def git(*args: str, check: bool = False) -> str:
    r = subprocess.run(["git", *args], check=False, cwd=ROOT, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "task"


def ensure_branch(p: dict) -> None:
    """Work on the task's own branch, cut from BASE (origin/main). Never on main."""
    branch = p["branch"]
    if branch in ("main", "master", ""):
        raise RuntimeError(f"refusing to work on branch {branch!r}")
    if git("branch", "--show-current") == branch:
        return
    if git("status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("working tree has uncommitted changes; commit or stash them first")
    if git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"):
        git("switch", branch, check=True)
    else:
        if BASE.startswith("origin/"):
            git("fetch", "origin", BASE.removeprefix("origin/"), check=True)
        git("switch", "-c", branch, BASE, check=True)  # works inside a worktree, unlike `switch main`
    log(f"on branch {branch}")


def changed_paths() -> list[str]:
    """Everything that differs from BASE: commits on the branch plus the working tree."""
    base = git("merge-base", "HEAD", BASE) or "HEAD"
    paths = set(git("diff", "--name-only", base).splitlines())
    paths |= {line[3:].split(" -> ")[-1] for line in git("status", "--porcelain").splitlines()}
    return sorted(x for x in paths if x)


def guard_violations() -> list[str]:
    out = []
    for path in changed_paths():
        if any(path == x or path.startswith(x) for x in PROTECTED):
            out.append(f"{path} is protected (it decides whether your work passes); revert your change to it")
        if any(path == x or path.startswith(x) for x in FORBIDDEN):
            out.append(f"{path} holds user data or secrets; remove it from your changes and never stage it")
    return out


# ---------------------------------------------------------------- context

def context(p: dict) -> str:
    """The execution context handed to the agent each iteration."""
    feedback = p["last_feedback"].strip()
    if len(feedback) > FEEDBACK_MAX_CHARS:
        feedback = "... (earlier output cut)\n" + feedback[-FEEDBACK_MAX_CHARS:]
    recent = [f"  #{h['iteration']}: {h['result']}" for h in p["history"][-5:]]
    return "\n".join([
        "You are an unattended coding agent working in the Polytone repo. No human will answer questions",
        "during this run. Follow CLAUDE.md, except: do NOT push, open PRs or merge (the orchestrator does).",
        "",
        f"TASK: {p['task']}",
        f"DETAILS: {p['details'] or '(none)'}",
        f"ITERATION: {p['iteration']} of {p['max_iterations']}",
        f"BRANCH: {p['branch']} (already checked out; stay on it)",
        "",
        "GIT STATE:",
        git("status", "--short") or "  (clean)",
        "Commits on this branch:",
        git("log", "--oneline", f"{BASE}..HEAD") or "  (none yet)",
        "",
        "PREVIOUS ITERATIONS:" if recent else "PREVIOUS ITERATIONS: none, this is the first attempt.",
        *recent,
        "",
        "LAST VALIDATION FEEDBACK (fix all of this first; it is the real output of validate.py):"
        if feedback else "LAST VALIDATION FEEDBACK: none.",
        feedback,
        "",
        "RULES:",
        "1. Make the smallest change that completes the task. Add or update tests for it.",
        "2. Before finishing, run `python3 validate.py` and make it pass.",
        "3. Commit with `git add <specific paths>` then `git commit -m ...` (never `git add -A`).",
        "4. Never edit validate.py, orchestrator.py, progress.json or .github/. Never read or touch data/,",
        "   .env, credentials.json, token.json, the macOS Keychain, or the real Neo4j (port 7687).",
        "5. Do not weaken, skip or delete tests to make validation pass.",
        "6. If the task is impossible, unsafe or too ambiguous to do without a human, stop and make the",
        "   first line of your final message `BLOCKED: <one-sentence reason>`.",
        "7. End with a 1-3 line summary of what you changed.",
    ])


# ---------------------------------------------------------------- steps

def run_agent(p: dict, prompt: str) -> dict:
    """Run one headless agent turn. Returns {ok, result, cost_usd, error}."""
    base = shlex.split(os.environ.get("POLYTONE_AGENT_CMD", "claude -p"))  # override: tests / other agents
    cmd = [*base, prompt, *AGENT_ARGS]
    if p.get("budget_usd_per_iteration"):
        cmd += ["--max-budget-usd", str(p["budget_usd_per_iteration"])]
    LOGS.mkdir(parents=True, exist_ok=True)
    stem = LOGS / f"{time.strftime('%Y%m%d-%H%M%S')}-iter{p['iteration']}"
    stem.with_suffix(".prompt.txt").write_text(prompt)
    try:
        r = subprocess.run(cmd, check=False, cwd=ROOT, capture_output=True, text=True, timeout=AGENT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {"ok": False, "result": "", "cost_usd": 0.0, "error": f"agent timed out after {AGENT_TIMEOUT_S}s"}
    except FileNotFoundError:
        return {"ok": False, "result": "", "cost_usd": 0.0, "error": f"{base[0]}: command not found"}
    stem.with_suffix(".out.json").write_text(r.stdout)
    if r.stderr:
        stem.with_suffix(".err.txt").write_text(r.stderr)
    try:
        out = json.loads(r.stdout)
    except json.JSONDecodeError:
        out = {"result": r.stdout, "is_error": r.returncode != 0}
    ok = r.returncode == 0 and not out.get("is_error")
    return {"ok": ok, "result": str(out.get("result") or ""), "cost_usd": float(out.get("total_cost_usd") or 0),
            "error": "" if ok else (r.stderr.strip()[-2000:] or str(out.get("result"))[-2000:] or f"exit {r.returncode}")}


def run_validate() -> tuple[bool, str]:
    r = subprocess.run([sys.executable, str(ROOT / "validate.py")], check=False, cwd=ROOT, text=True)
    fb = AGENT_DIR / "feedback.log"
    return r.returncode == 0, fb.read_text() if fb.exists() else ""


def open_pr(p: dict) -> None:
    git("push", "-u", "origin", p["branch"], check=True)
    r = subprocess.run(["gh", "pr", "create", "--fill", "--head", p["branch"]], check=False, cwd=ROOT, capture_output=True,
                       text=True)
    url = (r.stdout.strip().splitlines() or [""])[-1] if r.returncode == 0 else ""
    save(p, pr_url=url)
    log(f"PR: {url}" if url else f"PR not created: {r.stderr.strip()}")


def stop_requested() -> bool:
    return STOP_FILE.exists()


def record(p: dict, result: str, started: float, cost: float = 0.0) -> None:
    p["history"].append({"iteration": p["iteration"], "result": result, "seconds": round(time.monotonic() - started),
                         "cost_usd": round(cost, 4), "at": now()})
    p["total_cost_usd"] = round(p["total_cost_usd"] + cost, 4)


def work(p: dict, open_pr_on_pass: bool) -> None:
    """Iterate on the current task until it passes, blocks, stops or runs out of iterations."""
    ensure_branch(p)
    agent_errors = 0
    while True:
        if stop_requested():
            save(p, status="stopped")
            log("stopped (STOP file)")
            return
        if p["iteration"] >= p["max_iterations"]:
            save(p, status="blocked", last_feedback=f"Hit max_iterations ({p['max_iterations']}). "
                                                    + p["last_feedback"][-2000:])
            log("blocked: max iterations reached; see last_feedback")
            return

        save(p, iteration=p["iteration"] + 1, status="running")
        started = time.monotonic()
        log(f"iteration {p['iteration']}/{p['max_iterations']}: agent working…")
        a = run_agent(p, context(p))

        if not a["ok"]:
            agent_errors += 1
            record(p, f"agent error: {a['error'][:200]}", started, a["cost_usd"])
            save(p, status="failed", last_feedback=f"The previous agent run crashed: {a['error']}")
            log(f"agent error ({agent_errors}/{MAX_AGENT_ERRORS}): {a['error'][:300]}")
            if agent_errors >= MAX_AGENT_ERRORS:
                save(p, status="blocked")
                log("blocked: agent keeps failing (auth? rate limit? budget?)")
                return
            time.sleep(60 * agent_errors)  # back off before retrying
            continue
        agent_errors = 0

        if a["result"].lstrip().startswith("BLOCKED:"):
            reason = a["result"].strip().splitlines()[0]
            record(p, reason, started, a["cost_usd"])
            save(p, status="blocked", last_feedback=reason)
            log(reason)
            return

        if violations := guard_violations():
            record(p, "guard: touched protected files", started, a["cost_usd"])
            save(p, status="failed", last_feedback="GUARD FAILED, nothing was validated:\n- " + "\n- ".join(violations))
            log("guard failed: " + "; ".join(violations))
            continue

        save(p, status="validating")
        log("validating…")
        passed, fb = run_validate()
        if passed and git("status", "--porcelain", "--untracked-files=normal"):
            passed, fb = False, "Validation passed but the work is not committed. Commit it:\n" + git("status", "--short")
        if not passed:
            record(p, "validation failed", started, a["cost_usd"])
            save(p, status="failed", last_feedback=fb)
            log("validation failed; feeding the log back")
            continue

        record(p, "passed", started, a["cost_usd"])
        save(p, status="passed", last_feedback="")
        log(f"PASSED after {p['iteration']} iteration(s), ${p['total_cost_usd']:.2f} total")
        if open_pr_on_pass:
            open_pr(p)
        return


# ---------------------------------------------------------------- commands

def cmd_new(a: argparse.Namespace) -> None:
    p = load()
    if p["status"] in ("running", "validating"):
        sys.exit(f"a task is {p['status']}: {p['task']!r}. `stop` it first.")
    p = dict(TEMPLATE, history=[])
    save(p, task=a.task, details=a.details or "", branch=a.branch or f"agent/{slug(a.task)}",
         max_iterations=a.max_iterations, budget_usd_per_iteration=a.budget, status="pending")
    STOP_FILE.unlink(missing_ok=True)
    print(f"queued: {a.task!r} on {p['branch']} (max {a.max_iterations} iterations). Next: python3 orchestrator.py run")


def cmd_run(a: argparse.Namespace) -> None:
    STOP_FILE.unlink(missing_ok=True)
    while True:
        p = load()
        if p["status"] in ("pending", "running", "validating", "failed"):  # running = resumed after a crash
            try:
                work(p, a.pr)
            except RuntimeError as e:
                save(p, status="blocked", last_feedback=str(e))
                log(f"blocked: {e}")
        if not a.watch or stop_requested():
            return
        log(f"idle ({load()['status']}); waiting for `orchestrator.py new` …")
        while load()["status"] not in ("pending",) and not stop_requested():
            time.sleep(WATCH_POLL_S)
        if stop_requested():
            log("stopped")
            return


def cmd_status(_: argparse.Namespace) -> None:
    p = load()
    print(f"{p['status'].upper()}  {p['task'] or '(no task)'}")
    print(f"branch {p['branch'] or '-'} · iteration {p['iteration']}/{p['max_iterations']} · "
          f"${p['total_cost_usd']:.2f} · updated {p['updated_at'] or '-'}")
    if p["pr_url"]:
        print(f"PR {p['pr_url']}")
    if p["last_feedback"]:
        print("last feedback:\n  " + "\n  ".join(p["last_feedback"].strip().splitlines()[:15]))


def cmd_stop(_: argparse.Namespace) -> None:
    AGENT_DIR.mkdir(exist_ok=True)
    STOP_FILE.touch()
    print("stop requested: the loop exits after the current step (an agent run in progress finishes first)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new", help="queue a task (replaces the previous one)")
    n.add_argument("task")
    n.add_argument("--details", help="acceptance criteria, files to look at, constraints")
    n.add_argument("--branch", help="default: agent/<task-slug>")
    n.add_argument("--max-iterations", type=int, default=TEMPLATE["max_iterations"])
    n.add_argument("--budget", type=float, default=TEMPLATE["budget_usd_per_iteration"],
                   help="max USD per agent run (claude --max-budget-usd)")
    r = sub.add_parser("run", help="work the queued task")
    r.add_argument("--watch", action="store_true", help="always-on: then wait for the next task")
    r.add_argument("--pr", action="store_true", help="on pass: push and open a PR (never merges)")
    sub.add_parser("status")
    sub.add_parser("stop")
    a = ap.parse_args()
    {"new": cmd_new, "run": cmd_run, "status": cmd_status, "stop": cmd_stop}[a.cmd](a)


if __name__ == "__main__":
    main()
