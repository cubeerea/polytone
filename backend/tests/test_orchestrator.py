"""orchestrator.py: implementor → validate → evaluator loop, no-op tasks and routine queueing.

Everything runs in a throwaway git repo (with a bare "origin") and fake agents: no Claude, no gh,
and the real repo's branches and progress.json are never touched.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import orchestrator as orch


def sh(cwd: Path, *args: str) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    sh(tmp_path, "git", "init", "-q", "--bare", "-b", "main", str(origin))
    sh(tmp_path, "git", "clone", "-q", str(origin), str(work))
    for k, v in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
        sh(work, "git", "config", k, v)
    (work / ".gitignore").write_text("progress.json*\n.agent/\n")
    (work / "app.py").write_text("x = 1\n")
    sh(work, "git", "add", ".")
    sh(work, "git", "commit", "-qm", "init")
    sh(work, "git", "push", "-q", "origin", "main")

    agent_dir = work / ".agent"
    for name, value in {
        "ROOT": work, "PROGRESS": work / "progress.json", "AGENT_DIR": agent_dir, "LOGS": agent_dir / "logs",
        "ARCHIVE": agent_dir / "history", "ROUTINE_STATE": agent_dir / "routine.json",
        "STOP_FILE": agent_dir / "STOP", "BASE": "origin/main",
    }.items():
        monkeypatch.setattr(orch, name, value)
    monkeypatch.setattr(orch.time, "sleep", lambda _s: None)
    monkeypatch.setattr(orch, "run_validate", lambda: (True, ""))
    return work


def implementor(work: Path, results: list[str], commit: bool = True):
    """Fake implementor: commits one edit per call (if `commit`) and answers with the next result."""
    calls = iter(results)

    def run(p, _prompt):
        if commit:
            (work / "app.py").write_text(f"x = {p['iteration'] + 1}\n")
            sh(work, "git", "commit", "-qam", f"edit {p['iteration']}")
        return {"ok": True, "result": next(calls), "cost_usd": 0.1, "error": ""}

    return run


def evaluator(replies: list[str], prompts: list[str]):
    calls = iter(replies)

    def run(prompt, args, _budget, _label, _timeout):
        assert "Edit" in args[args.index("--disallowedTools"):]  # read-only
        prompts.append(prompt)
        return {"ok": True, "result": next(calls), "cost_usd": 0.05, "error": ""}

    return run


def test_evaluator_rejection_is_fed_back_then_approval_passes(repo, monkeypatch):
    prompts: list[str] = []
    monkeypatch.setattr(orch, "run_agent", implementor(repo, ["did it", "fixed the review"]))
    monkeypatch.setattr(orch, "run_claude", evaluator(
        ["VERDICT: REJECT\n1. app.py:1 off by one", "VERDICT: APPROVE"], prompts))
    p = orch.queue_task("Bump x", "make x bigger", max_iterations=4)

    orch.work(p, open_pr_on_pass=False)

    p = orch.load()
    assert p["status"] == "passed" and p["iteration"] == 2
    assert [h["result"] for h in p["history"]] == ["evaluator rejected", "passed"]
    assert p["total_cost_usd"] == pytest.approx(0.3)
    # the evaluator saw the task and the real diff, but not the implementor's own summary
    assert "TASK: Bump x" in prompts[0] and "+x = 2" in prompts[0] and "did it" not in prompts[0]
    assert sh(repo, "git", "branch", "--show-current") == "agent/bump-x"


def test_rejection_reasons_reach_the_next_implementor_prompt(repo, monkeypatch):
    seen: list[str] = []
    run = implementor(repo, ["one", "two"])

    def spy(p, prompt):
        seen.append(prompt)
        return run(p, prompt)

    monkeypatch.setattr(orch, "run_agent", spy)
    monkeypatch.setattr(orch, "run_claude", evaluator(["VERDICT: REJECT\n1. missing test for x", "VERDICT: APPROVE"], []))
    orch.work(orch.queue_task("Bump x"), open_pr_on_pass=False)
    assert "EVALUATOR REJECTED" in seen[1] and "missing test for x" in seen[1]


def test_evaluator_without_verdict_is_an_error_not_a_rejection(repo, monkeypatch):
    monkeypatch.setattr(orch, "run_agent", implementor(repo, ["a", "b", "c"]))
    monkeypatch.setattr(orch, "run_claude", evaluator(["looks fine to me"] * 3, []))
    orch.work(orch.queue_task("Bump x", max_iterations=5), open_pr_on_pass=False)
    p = orch.load()
    assert p["status"] == "blocked"
    assert all(h["result"].startswith("evaluator error") for h in p["history"])


def test_evaluator_that_changes_the_tree_blocks(repo, monkeypatch):
    def meddling(*_a):
        (repo / "app.py").write_text("x = 999\n")
        return {"ok": True, "result": "VERDICT: APPROVE", "cost_usd": 0, "error": ""}

    monkeypatch.setattr(orch, "run_agent", implementor(repo, ["done"]))
    monkeypatch.setattr(orch, "run_claude", meddling)
    with pytest.raises(RuntimeError, match="evaluator changed"):
        orch.work(orch.queue_task("Bump x"), open_pr_on_pass=False)


def test_nothing_to_do_passes_without_validation_or_review(repo, monkeypatch):
    monkeypatch.setattr(orch, "run_agent", implementor(repo, ["NOTHING TO DO: all checks clean"], commit=False))
    monkeypatch.setattr(orch, "run_validate", lambda: pytest.fail("validate should not run"))
    monkeypatch.setattr(orch, "run_claude", lambda *a: pytest.fail("evaluator should not run"))
    orch.work(orch.queue_task("Routine: validation sweep"), open_pr_on_pass=False)
    p = orch.load()
    assert p["status"] == "passed" and p["iteration"] == 1 and "all checks clean" in p["last_feedback"]


def test_passing_validation_without_commits_is_not_a_pass(repo, monkeypatch):
    monkeypatch.setattr(orch, "run_agent", implementor(repo, ["done", "done"], commit=False))
    monkeypatch.setattr(orch, "run_claude", lambda *a: pytest.fail("evaluator should not run"))
    orch.work(orch.queue_task("Bump x", max_iterations=2), open_pr_on_pass=False)
    p = orch.load()
    assert p["status"] == "blocked" and "no commits" in p["last_feedback"]


def test_no_evaluate_skips_the_evaluator(repo, monkeypatch):
    monkeypatch.setattr(orch, "run_agent", implementor(repo, ["done"]))
    monkeypatch.setattr(orch, "run_claude", lambda *a: pytest.fail("evaluator should not run"))
    orch.work(orch.queue_task("Bump x", evaluate=False), open_pr_on_pass=False)
    assert orch.load()["status"] == "passed"


@pytest.mark.parametrize(("text", "approved"), [
    ("VERDICT: APPROVE", True),
    ("**VERDICT: REJECT**\n1. bad", False),
    ("Some preamble\nverdict: approve\nfine", True),
    ("I think it is fine", None),
])
def test_parse_verdict(text, approved):
    assert orch.parse_verdict(text)[0] is approved


def test_routines_rotate_respect_interval_and_open_pr_cap(repo, monkeypatch):
    monkeypatch.setattr(orch, "open_routine_prs", lambda: 0)
    assert orch.maybe_queue_routine(every_s=3600)
    p = orch.load()
    assert p["routine"] == "validation" and p["branch"].startswith("agent/routine-validation-")
    assert p["max_iterations"] == orch.ROUTINE_LIMITS["max_iterations"]

    assert not orch.maybe_queue_routine(every_s=3600)  # still pending → not idle
    orch.save(p, status="passed")
    assert not orch.maybe_queue_routine(every_s=3600)  # idle, but not due yet

    state = json.loads(orch.ROUTINE_STATE.read_text())
    orch.ROUTINE_STATE.write_text(json.dumps({**state, "last_queued_at": time.time() - 7200}))
    monkeypatch.setattr(orch, "open_routine_prs", lambda: orch.ROUTINE_MAX_OPEN_PRS)
    assert not orch.maybe_queue_routine(every_s=3600)  # too many unreviewed routine PRs

    orch.ROUTINE_STATE.write_text(json.dumps({**state, "last_queued_at": 0}))
    monkeypatch.setattr(orch, "open_routine_prs", lambda: 0)
    assert orch.maybe_queue_routine(every_s=3600)
    assert orch.load()["routine"] == "tests"
    # the finished validation task was archived, not lost
    archived = [json.loads(f.read_text()) for f in orch.ARCHIVE.iterdir()]
    assert [a["task"] for a in archived] == ["Routine: validation sweep"]


def test_routine_never_replaces_an_active_task(repo, monkeypatch):
    monkeypatch.setattr(orch, "open_routine_prs", lambda: 0)
    orch.queue_task("Human task")
    for status in orch.ACTIVE:
        orch.save(orch.load(), status=status)
        assert not orch.maybe_queue_routine(every_s=0)
    assert orch.load()["task"] == "Human task"
