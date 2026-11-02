"""Learning evaluation: does what Polytone learned make drafts sound more like the user?

Edit learning (``edits.py``) and new samples keep changing a persona's constraints. This module
checks, now and then, that the change helped, and backs it out when it did not.

Harness
-------
- **Intents**: ``INTENTS``, a fixed set of short, neutral requests (no user text), always the same,
  so two evaluations compare like with like.
- **Holdout**: the persona's newest ``HOLDOUT_SIZE`` human samples (``samples``). They are in the
  style graph too, so the absolute score flatters every candidate a little; what matters is the
  comparison *between* candidates, which all see the same holdout. Fewer than ``MIN_HOLDOUT``
  samples → skipped with a reason.
- **Candidates**: ``current`` (graph + edit corrections — what generation uses with no override),
  ``no_corrections`` (graph only) and ``previous`` (the last *accepted* snapshot, if any).
- **Score**: for each candidate and intent, the prompt is built exactly as generation builds it
  (``select_examples`` → ``build_system_parts`` / ``build_user_prompt``, the persona's tone line, the
  configured draft speed), the draft is generated with ``rt.llm`` and post-processed like a real
  draft, then scored with ``style_distance.score`` against the holdout (feature-only: the topic of
  a neutral intent says nothing about style). Candidate score = mean over intents. Identical prompts
  are generated once (a persona without corrections costs one candidate less). Drafts made here are
  never stored.

Decision (hysteresis, ``MARGIN`` points)
----------------------------------------
The *incumbent* is the candidate generation currently uses: ``current`` (no override),
``previous`` (pinned) or ``no_corrections`` (corrections paused). The best other candidate replaces
it only when it scores at least ``MARGIN`` higher, so noise and ties never flip the state:

- winner ``current``        → status ``accepted``: this snapshot becomes the new "previous"; any
                              override is cleared.
- winner ``previous``       → status ``rolled_back``: generation is pinned to the previous snapshot's
                              constraints until the next evaluation (or ``DELETE /learning/state``).
- winner ``no_corrections`` → status ``corrections_paused``: generation ignores edit corrections for
                              the persona until the next evaluation.

Every evaluation that ran stores one ``constraint_snapshots`` row: the ``current`` constraints
(encrypted), its score, and in ``eval_json`` the per-candidate scores, the incumbent, the winner and
the data watermark (numbers and labels only).

Cost
----
A real-model evaluation costs up to 3 candidates × ``len(INTENTS)`` calls. ``run_due`` (the daily
background task) only evaluates personas with ≥ ``MIN_HOLDOUT`` samples whose last evaluation is at
least ``learning_eval_days`` (config, default 7, 0 = never automatically) old AND that gained a
sample or a learned edit since. ``POST /learning/evaluate`` runs one on demand.
"""
from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

from backend.learning import state as learning_state

log = logging.getLogger(__name__)

INTENTS: tuple[str, ...] = (
    "tell sam the meeting moved to three",
    "ask for the latest numbers before friday",
    "thanks for the help with the launch, it went really well",
    "running ten minutes late, start without me",
    "can't make dinner tonight, maybe next week instead",
    "the report is done, sending it over this afternoon",
)
HOLDOUT_SIZE = 20
MIN_HOLDOUT = 5
MARGIN = 2.0  # points (0-100 scale) a challenger must win by
KIND = "evaluation"
CANDIDATES = ("current", "no_corrections", "previous")
ACCEPTED, ROLLED_BACK, PAUSED = "accepted", "rolled_back", "corrections_paused"
SKIPPED, FAILED = "skipped", "failed"
_STATUS = {"current": ACCEPTED, "previous": ROLLED_BACK, "no_corrections": PAUSED}
DAY_S = 24 * 3600
FIRST_RUN_DELAY_S = 600  # the background task waits this long after start-up (warm-up, first drafts)
DEFAULT_EVAL_DAYS = 7
MAX_EVAL_DAYS = 90

_RUN_LOCK = threading.Lock()  # one evaluation at a time per process


class EvaluationBusy(RuntimeError):
    """Another evaluation is running."""


# --------------------------------------------------------------------------- decision
def incumbent_for(mode: str, has_previous: bool) -> str:
    if mode == learning_state.PINNED and has_previous:
        return "previous"
    if mode == learning_state.PAUSED:
        return "no_corrections"
    return "current"


def decide(scores: dict[str, float], incumbent: str, margin: float = MARGIN) -> str:
    """The candidate generation should use next. ``scores`` holds the candidates that ran."""
    if incumbent not in scores:
        incumbent = "current"
    best = incumbent
    for name in CANDIDATES:  # fixed order: ties between challengers are deterministic
        wins = name in scores and name != incumbent and scores[name] - scores[incumbent] >= margin
        if wins and (best == incumbent or scores[name] > scores[best]):
            best = name
    return best


# --------------------------------------------------------------------------- candidates
def _previous(db: Any, persona: str) -> dict[str, Any] | None:
    rows = db.list_snapshots(persona, 1, status=ACCEPTED, with_constraints=True)
    if rows and isinstance(rows[0].get("constraints"), dict):
        return rows[0]
    return None


def candidates(rt: Any, persona: str) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None]:
    """``({name: constraints}, previous snapshot row | None)``."""
    from backend.learning.edits import apply_corrections

    graph = dict(rt.store.get_constraints(persona))
    out = {
        "current": apply_corrections(dict(graph), rt.db.get_corrections(persona)),
        "no_corrections": graph,
    }
    prev = None
    try:
        prev = _previous(rt.db, persona)
    except Exception as e:  # noqa: BLE001 - an unreadable snapshot just drops the candidate
        log.warning("previous learning snapshot unreadable: %s", type(e).__name__)
    if prev is not None:
        out["previous"] = prev["constraints"]
    return out, prev


def _draft(rt: Any, persona: str, constraints: dict[str, Any], intent: str, guidance: str | None,
           model: str | None, memo: dict[tuple[str, str], str]) -> str:
    """Generate one draft exactly as the generate graph would (without recording it)."""
    from backend.langgraph.examples import select_examples
    from backend.langgraph.generate_graph import postprocess_text
    from backend.langgraph.prompts import build_system_parts, build_user_prompt

    examples = select_examples(rt.db, persona, intent, constraints)
    parts = build_system_parts(constraints, persona, None, examples, guidance=guidance)
    user = build_user_prompt(intent)
    joined = "\n\n".join(p for p in parts if p)
    key = (joined, user)
    if key in memo:
        return memo[key]
    system = parts if getattr(rt.llm, "cache_system", False) else joined
    raw = rt.llm.complete(system, user, model=model) if model else rt.llm.complete(system, user)
    memo[key] = postprocess_text(raw)
    return memo[key]


def score_candidate(rt: Any, persona: str, constraints: dict[str, Any], holdout: list[str], *,
                    guidance: str | None, model: str | None, memo: dict[tuple[str, str], str]) -> list[float]:
    from backend.evaluation.style_distance import score

    out = []
    for intent in INTENTS:
        text = _draft(rt, persona, constraints, intent, guidance, model, memo)
        out.append(float(score(text, holdout, persona, use_embeddings=False)["score"]))
    return out


# --------------------------------------------------------------------------- evaluation
def _llm_unavailable(rt: Any) -> bool:
    return getattr(rt.llm, "name", "") == "mock" and rt.settings.llm_backend != "mock"


def _state_out(db: Any, persona: str) -> dict[str, Any]:
    row = None
    try:
        row = db.get_learning_state(persona)
    except Exception as e:  # noqa: BLE001
        log.warning("learning state unreadable: %s", type(e).__name__)
    if not row:
        return {"mode": learning_state.NORMAL, "snapshot_id": None, "since": None}
    return {"mode": row["mode"], "snapshot_id": row.get("snapshot_id"), "since": row.get("updated_at")}


def _result(persona: str, status: str, reason: str | None = None, **kw: Any) -> dict[str, Any]:
    return {"persona": persona, "status": status, "reason": reason, "snapshot_id": None, "decision": None,
            "scores": {}, "holdout": 0, "llm_calls": 0, "margin": MARGIN, **kw}


def evaluate(rt: Any, persona: str, *, trigger: str = "manual", actor: str = "backend") -> dict[str, Any]:
    """Run one evaluation for ``persona`` (see module docstring). Raises ``EvaluationBusy`` while
    another runs. Never raises for missing data or a failing model: returns ``status``
    ``skipped``/``failed`` with a ``reason`` instead."""
    if not _RUN_LOCK.acquire(blocking=False):
        raise EvaluationBusy("an evaluation is already running")
    try:
        return _evaluate(rt, persona, trigger, actor)
    finally:
        _RUN_LOCK.release()


def _evaluate(rt: Any, persona: str, trigger: str, actor: str) -> dict[str, Any]:
    from backend import personas
    from backend.llm.client import FAST_MODEL

    db = rt.db
    holdout = [s["text"] for s in db.list_samples(persona=persona, limit=HOLDOUT_SIZE) if s.get("text")]
    if len(holdout) < MIN_HOLDOUT:
        return _result(persona, SKIPPED, f"needs at least {MIN_HOLDOUT} writing samples for this persona "
                                         f"(has {len(holdout)})", holdout=len(holdout),
                       state=_state_out(db, persona))
    if _llm_unavailable(rt):
        return _result(persona, SKIPPED, "no LLM configured", holdout=len(holdout), state=_state_out(db, persona))
    watermark = db.learning_watermark(persona)
    try:
        cands, prev = candidates(rt, persona)
    except Exception as e:  # noqa: BLE001 - graph unreachable
        log.warning("learning evaluation: constraints unavailable (%s)", type(e).__name__)
        return _result(persona, FAILED, "style graph unavailable", holdout=len(holdout), state=_state_out(db, persona))
    model = FAST_MODEL if db.get_config().get("draft_speed") == "fast" else None
    guidance = personas.guidance(db, persona)
    memo: dict[tuple[str, str], str] = {}
    per_intent: dict[str, list[float]] = {}
    try:
        for name in CANDIDATES:
            if name in cands:
                per_intent[name] = score_candidate(rt, persona, cands[name], holdout,
                                                   guidance=guidance, model=model, memo=memo)
    except Exception as e:  # noqa: BLE001 - a failed model call: nothing is decided or stored
        log.warning("learning evaluation: generation failed (%s)", type(e).__name__)
        return _result(persona, FAILED, "the model failed during evaluation", holdout=len(holdout),
                       llm_calls=len(memo), state=_state_out(db, persona))
    scores = {k: round(sum(v) / len(v), 2) for k, v in per_intent.items()}

    st = _state_out(db, persona)
    pinned_ok = prev is not None and st["mode"] == learning_state.PINNED and st["snapshot_id"] == prev["id"]
    incumbent = incumbent_for(st["mode"], pinned_ok)  # a pin to a snapshot that is gone → current
    winner = decide(scores, incumbent)
    status = _STATUS[winner]
    evaluation = {
        "scores": scores,
        "per_intent": {k: [round(x, 1) for x in v] for k, v in per_intent.items()},
        "incumbent": incumbent,
        "decision": winner,
        "winner_score": scores[winner],
        "margin": MARGIN,
        "holdout": len(holdout),
        "intents": len(INTENTS),
        "llm_calls": len(memo),
        "previous_snapshot_id": prev["id"] if prev else None,
        "watermark": watermark,
        "trigger": trigger,
    }
    snap_id = db.insert_snapshot(persona=persona, kind=KIND, status=status, constraints=cands["current"],
                                 score=scores["current"], evaluation=evaluation)
    if winner == "previous" and prev is not None:
        db.set_learning_state(persona, learning_state.PINNED, prev["id"])
    elif winner == "no_corrections":
        db.set_learning_state(persona, learning_state.PAUSED)
    else:
        db.set_learning_state(persona, None)
    learning_state.invalidate(persona)
    rt.audit_event("learning.evaluate", {
        "persona": persona, "status": status, "incumbent": incumbent, "trigger": trigger,
        "scores": scores, "holdout": len(holdout), "llm_calls": len(memo), "snapshot_id": snap_id,
    }, actor)
    log.info("learning evaluation %s: %s (scores %s)", persona, status, scores)
    return _result(persona, status, None, snapshot_id=snap_id, decision=winner, incumbent=incumbent,
                   scores=scores, holdout=len(holdout), llm_calls=len(memo), state=_state_out(db, persona))


def clear_override(rt: Any, persona: str, actor: str = "ui") -> dict[str, Any]:
    """Undo a pin / pause by hand: generation goes back to graph + corrections."""
    before = _state_out(rt.db, persona)
    rt.db.set_learning_state(persona, None)
    learning_state.invalidate(persona)
    if before["mode"] != learning_state.NORMAL:
        rt.audit_event("learning.state_cleared", {"persona": persona, "mode": before["mode"]}, actor)
    return _state_out(rt.db, persona)


# --------------------------------------------------------------------------- reporting
def history(db: Any, persona: str, limit: int = 20) -> list[dict[str, Any]]:
    out = []
    for r in db.list_snapshots(persona, limit):
        ev = r.get("evaluation") or {}
        out.append({
            "id": r["id"],
            "created_at": r["created_at"],
            "status": r["status"],
            "decision": ev.get("decision"),
            "incumbent": ev.get("incumbent"),
            "score": ev.get("winner_score", r.get("score")),
            "scores": ev.get("scores") or {},
            "holdout": ev.get("holdout"),
            "trigger": ev.get("trigger"),
        })
    return out


def summary(db: Any, persona: str) -> dict[str, Any]:
    """The evaluation part of ``GET /learning``."""
    try:
        last = history(db, persona, 1)
    except Exception as e:  # noqa: BLE001
        log.warning("learning history unreadable: %s", type(e).__name__)
        last = []
    return {
        "state": _state_out(db, persona),
        "last_evaluation": last[0] if last else None,
        "eval_every_days": eval_days(db),
        "min_holdout": MIN_HOLDOUT,
    }


# --------------------------------------------------------------------------- scheduling
def eval_days(db: Any) -> int:
    try:
        return max(0, min(MAX_EVAL_DAYS, int(db.get_config().get("learning_eval_days", DEFAULT_EVAL_DAYS))))
    except (TypeError, ValueError):
        return DEFAULT_EVAL_DAYS


def _parse(ts: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


def due(db: Any, persona: str, days: int, now: datetime | None = None) -> tuple[bool, str]:
    """Whether the background task should evaluate ``persona`` now, and why (not)."""
    if days <= 0:
        return False, "automatic evaluation is off"
    wm = db.learning_watermark(persona)
    if wm["samples"] < MIN_HOLDOUT:
        return False, "too few samples"
    last = db.list_snapshots(persona, 1)
    if not last:
        return True, "never evaluated"
    created = _parse(last[0]["created_at"])
    if created is not None and (now or datetime.now(UTC)) - created < timedelta(days=days):
        return False, "evaluated recently"
    before = (last[0].get("evaluation") or {}).get("watermark") or {}
    if before.get("sample_max_id") == wm["sample_max_id"] and before.get("edits") == wm["edits"]:
        return False, "nothing new since the last evaluation"
    return True, "new samples or edits"


def run_due(rt: Any, now: datetime | None = None) -> list[dict[str, Any]]:
    """Evaluate every persona that is due (see ``due``). Returns the results."""
    days = eval_days(rt.db)
    if days <= 0 or _llm_unavailable(rt):
        return []
    out = []
    for persona in sorted(rt.db.count_samples_by_persona()):
        ok, _why = due(rt.db, persona, days, now)
        if not ok:
            continue
        try:
            out.append(evaluate(rt, persona, trigger="scheduled"))
        except EvaluationBusy:
            break  # an on-demand run is going; try again tomorrow
    return out


class EvaluationTask:
    """After ``first_delay_s``, run ``run_due`` every ``interval_s`` until ``stop()``."""

    def __init__(self, rt: Any, interval_s: float = DAY_S, first_delay_s: float = FIRST_RUN_DELAY_S) -> None:
        self.rt = rt
        self.interval_s = interval_s
        self.first_delay_s = first_delay_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="polytone-learning-eval", daemon=True)

    def start(self) -> EvaluationTask:
        self._thread.start()
        return self

    def _run(self) -> None:
        if self._stop.wait(self.first_delay_s):
            return
        while not self._stop.is_set():
            try:
                run_due(self.rt)
            except Exception as e:  # noqa: BLE001 - try again next round
                log.warning("learning evaluation run failed: %s", e.__class__.__name__)
            self._stop.wait(self.interval_s)

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
