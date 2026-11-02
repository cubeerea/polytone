"""Phase 3 — show that learning helps (backend/learning/evaluate.py, state.py, outcomes.py).

Evaluations score candidate constraints (graph + corrections, graph only, last accepted snapshot)
on fixed intents against the persona's newest samples, then accept, roll back or pause corrections
with a margin. ``FollowLLM`` makes the outcome predictable offline: it writes in the user's casual
voice only when the system prompt asks for it, so the candidate whose rules say "lowercase, use
contractions" scores far closer to the casual holdout than one that says "sentence case".
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from backend.graph.store import InMemoryGraphStore
from backend.ingestion.extract import extract
from backend.ingestion.sources import ingest_typed
from backend.langgraph import examples
from backend.langgraph.examples import select_examples
from backend.langgraph.generate_graph import run_generation
from backend.learning import evaluate, outcomes
from backend.learning import state as learning_state
from backend.llm.client import join_system
from backend.runtime import build_runtime, set_runtime
from backend.security import crypto
from backend.storage.db import ENC_COLUMNS, Database

from .conftest import api_client

SLACK = "com.tinyspeck.slackmacgap"

CASUAL = [
    "hey — i don't think we can make it today. let's push to friday! thanks so much.",
    "honestly it's fine. i'll send the deck later — no rush.",
    "so the build broke again... i'm looking into it now. should be fixed by lunch.",
    "yeah i can't do monday — can we try tuesday? i'll bring the notes.",
    "ok cool, i'm on it. won't take long — maybe an hour.",
    "ha that's wild. i'll call you tonight, don't worry about it.",
    "sure thing — i'll ping sam and get back to you by noon.",
]
FORMAL = [
    "Dear team, I would like to inform you that the quarterly review has been rescheduled to Thursday.",
    "Please find attached the requested documents. I remain available should you require anything further.",
    "Thank you for your patience. We will provide a comprehensive update at the earliest opportunity.",
    "I am writing to confirm our appointment on Monday. Kindly let me know if the time is convenient.",
    "We appreciate your continued support. The final report will be circulated by the end of the week.",
]

BEST = "hey — i'm running a bit late, don't wait for me. i'll send it over later — no rush!"
MEDIUM = "hey — i'm running a bit late, do not wait for me. i will send it over later — no rush!"
WORST = ("Dear colleague, I would like to inform you that I am currently unable to attend. Please accept my "
         "sincere apologies for any inconvenience this may cause. Kind regards.")


class FollowLLM:
    """Deterministic fake that follows the casing / contraction rules of the system prompt."""

    name = "mock"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, system, user, model=None) -> str:
        self.calls += 1
        s = join_system(system)
        if "Casing: all lowercase" not in s:
            return WORST
        return BEST if ": use contractions" in s else MEDIUM


@pytest.fixture
def rt(settings, tmp_path):
    r = build_runtime(settings, db_path=tmp_path / "eval.sqlite3", store=InMemoryGraphStore(), llm=FollowLLM(),
                      scorer=False)
    set_runtime(r)
    learning_state.invalidate()
    yield r
    set_runtime(None)
    learning_state.invalidate()
    r.close()


def _graph(r, texts, persona="Default") -> None:
    r.store = InMemoryGraphStore()
    for t in texts:
        r.store.write_extraction(extract(t), persona)


def _samples(r, texts=CASUAL, persona="Default") -> None:
    for t in texts:
        r.db.insert_sample(text=t, source="manual", persona=persona, provenance={"source": "manual"})


def _snapshot(r, constraints, persona="Default", status=evaluate.ACCEPTED) -> int:
    return r.db.insert_snapshot(persona=persona, kind="evaluation", status=status, constraints=constraints,
                                score=50.0, evaluation={"scores": {"current": 50.0}})


def _casual_constraints(r, **over):
    _graph(r, CASUAL)
    c = dict(r.store.get_constraints("Default"))
    c.update(over)
    return c


def _correct_casing_to_sentence(r, persona="Default") -> None:
    for _ in range(4):  # ≥ MIN_EDITS edits that all re-capitalise the draft
        r.db.update_corrections(persona, [("edits", "", None, 1.0), ("lower", "", None, -0.9)])
    from backend.learning import edits

    edits.invalidate(persona)


# ------------------------------------------------------------------ decision rule
def test_decide_needs_the_margin_and_respects_the_incumbent():
    assert evaluate.decide({"current": 60, "no_corrections": 61.9, "previous": 59}, "current") == "current"
    assert evaluate.decide({"current": 60, "previous": 62}, "current") == "previous"
    assert evaluate.decide({"current": 60, "no_corrections": 63, "previous": 64}, "current") == "previous"
    # an override stays until something beats it by the margin
    assert evaluate.decide({"current": 61, "previous": 60}, "previous") == "previous"
    assert evaluate.decide({"current": 62, "previous": 60}, "previous") == "current"
    assert evaluate.decide({"current": 70, "no_corrections": 69}, "no_corrections") == "no_corrections"
    assert evaluate.decide({"current": 70}, "previous") == "current"  # incumbent did not run


def test_fake_llm_orders_the_candidates():
    from backend.evaluation.style_distance import score

    s = {k: score(t, CASUAL, use_embeddings=False)["score"] for k, t in
         (("best", BEST), ("medium", MEDIUM), ("worst", WORST))}
    assert s["best"] >= s["medium"] + evaluate.MARGIN and s["medium"] >= s["worst"] + evaluate.MARGIN


# ------------------------------------------------------------------ snapshots
def test_first_evaluation_accepts_and_snapshot_is_encrypted(rt):
    _graph(rt, CASUAL)
    _samples(rt)
    res = evaluate.evaluate(rt, "Default")
    assert res["status"] == "accepted" and res["decision"] == "current"
    assert set(res["scores"]) == {"current", "no_corrections"}  # no previous snapshot yet
    # no corrections → identical prompts are generated once
    assert res["llm_calls"] == len(evaluate.INTENTS) == rt.llm.calls
    with rt.db.connect() as c:
        raw = c.execute("SELECT constraints_json, eval_json FROM constraint_snapshots").fetchone()
    assert crypto.is_encrypted(raw["constraints_json"])
    snap = rt.db.get_snapshot(res["snapshot_id"])
    words = snap["constraints"]["top_openers"]  # the user's words, in the decrypted snapshot
    assert words and not any(w in raw["constraints_json"] or w in raw["eval_json"] for w in words)
    assert "casing" not in raw["constraints_json"]
    assert snap["constraints"]["casing"] == "lower" and snap["status"] == "accepted"
    assert snap["evaluation"]["watermark"]["samples"] == len(CASUAL)
    # the key probe covers the new table: a different data key refuses to open the file
    assert ENC_COLUMNS["constraint_snapshots"] == ("constraints_json",)


def test_too_few_holdout_samples_skips(rt):
    _graph(rt, CASUAL)
    _samples(rt, CASUAL[: evaluate.MIN_HOLDOUT - 1])
    res = evaluate.evaluate(rt, "Default")
    assert res["status"] == "skipped" and "at least" in res["reason"]
    assert rt.db.list_snapshots("Default") == [] and rt.llm.calls == 0


def test_current_beats_previous_is_accepted(rt):
    prev = _casual_constraints(rt, casing="sentence")  # an older, worse rule set
    old = _snapshot(rt, prev)
    _samples(rt)
    res = evaluate.evaluate(rt, "Default")
    assert res["status"] == "accepted" and res["scores"]["current"] >= res["scores"]["previous"] + evaluate.MARGIN
    assert res["state"]["mode"] == "normal"
    assert rt.db.list_snapshots("Default", status="accepted")[0]["id"] == res["snapshot_id"] != old


def test_previous_wins_rolls_back_and_generation_uses_the_pin(rt):
    good = _casual_constraints(rt, contractions_rate=0.0)  # lowercase, no contractions: MEDIUM
    snap = _snapshot(rt, good)
    _graph(rt, FORMAL)  # new learning made it worse: sentence case → WORST
    _samples(rt)
    res = evaluate.evaluate(rt, "Default")
    assert res["status"] == "rolled_back" and res["decision"] == "previous"
    assert res["state"] == {"mode": "pinned", "snapshot_id": snap, "since": res["state"]["since"]}
    out = run_generation("tell sam the meeting moved to three", runtime=rt)
    assert out["constraints"] == good  # the pinned snapshot, not graph + corrections
    assert rt.store.get_constraints("Default")["casing"] != "lower"

    # same data again: the pin holds (and the accepted snapshot stays the old one)
    again = evaluate.evaluate(rt, "Default")
    assert again["status"] == "rolled_back" and again["incumbent"] == "previous"
    assert rt.db.list_snapshots("Default", status="accepted")[0]["id"] == snap

    # learning recovers (casual graph with contractions → BEST): the next evaluation clears the pin
    _graph(rt, CASUAL)
    fixed = evaluate.evaluate(rt, "Default")
    assert fixed["status"] == "accepted" and fixed["state"]["mode"] == "normal"
    assert run_generation("tell sam the meeting moved to three", runtime=rt)["constraints"]["contractions_rate"] > 0.5


def test_no_corrections_wins_pauses_corrections(rt, settings):
    _graph(rt, CASUAL)
    _samples(rt)
    _correct_casing_to_sentence(rt)  # edits push the rules the wrong way
    assert run_generation("tell sam the meeting moved to three", runtime=rt)["constraints"]["casing"] == "sentence"
    res = evaluate.evaluate(rt, "Default")
    assert res["status"] == "corrections_paused" and res["decision"] == "no_corrections"
    c = run_generation("tell sam the meeting moved to three", runtime=rt)["constraints"]
    assert c["casing"] == "lower" and "edits_learned" not in c  # corrections layer ignored
    with api_client(rt) as client:
        s = client.get("/learning").json()
        assert s["state"]["mode"] == "corrections_paused" and s["edits_learned"] == 4
        assert s["last_evaluation"]["status"] == "corrections_paused"
        # reversible by hand
        cleared = client.delete("/learning/state").json()
        assert cleared["state"]["mode"] == "normal"
    assert run_generation("tell sam the meeting moved to three", runtime=rt)["constraints"]["casing"] == "sentence"
    assert any(e["event"] == "learning.state_cleared" for e in rt.audit.entries(20))


def test_ties_never_flip_the_state(rt):
    _graph(rt, CASUAL)
    _samples(rt)
    same = dict(rt.store.get_constraints("Default"))
    from backend.learning.edits import apply_corrections

    snap = _snapshot(rt, apply_corrections(same, []))
    rt.db.set_learning_state("Default", "pinned", snap)  # pinned to constraints identical to current
    learning_state.invalidate()
    res = evaluate.evaluate(rt, "Default")
    assert res["scores"]["current"] == res["scores"]["previous"]
    assert res["status"] == "rolled_back" and res["state"]["mode"] == "pinned"  # tie: stays pinned
    rt.db.set_learning_state("Default", None)
    learning_state.invalidate()
    res = evaluate.evaluate(rt, "Default")
    assert res["status"] == "accepted" and res["state"]["mode"] == "normal"  # tie: stays normal


def test_failed_model_records_nothing(rt):
    _graph(rt, CASUAL)
    _samples(rt)

    def boom(system, user, model=None):
        raise TimeoutError("slow")

    rt.llm.complete = boom
    res = evaluate.evaluate(rt, "Default")
    assert res["status"] == "failed" and rt.db.list_snapshots("Default") == []


# ------------------------------------------------------------------ scheduling + endpoints
def test_schedule_only_evaluates_personas_with_new_data(rt):
    _graph(rt, CASUAL)
    _samples(rt)
    _samples(rt, ["tiny persona with only one sample here."], persona="Boss")
    now = datetime.now(UTC)
    first = evaluate.run_due(rt, now)
    assert [r["persona"] for r in first] == ["Default"]  # Boss has too few samples
    assert evaluate.run_due(rt, now) == []  # evaluated recently
    later = now + timedelta(days=8)
    assert evaluate.due(rt.db, "Default", 7, later) == (False, "nothing new since the last evaluation")
    assert evaluate.run_due(rt, later) == []
    _samples(rt, ["one more casual note — i'll be there at six, promise."])
    assert [r["persona"] for r in evaluate.run_due(rt, later)] == ["Default"]
    rt.db.update_corrections("Default", [("edits", "", None, 1.0)])  # a learned edit also counts
    assert evaluate.due(rt.db, "Default", 7, later + timedelta(days=8))[0]
    rt.db.set_config(learning_eval_days=0)
    assert evaluate.run_due(rt, later + timedelta(days=30)) == []


def test_evaluate_endpoints(rt):
    _graph(rt, CASUAL)
    _samples(rt)
    with api_client(rt) as client:
        r = client.post("/learning/evaluate", json={})
        assert r.status_code == 200 and r.json()["status"] == "accepted"
        r2 = client.post("/learning/evaluate", json={"persona": "default"})  # case-insensitive
        assert r2.json()["persona"] == "Default"
        hist = client.get("/learning/evaluations?persona=Default").json()
        assert hist["persona"] == "Default" and len(hist["evaluations"]) == 2
        top = hist["evaluations"][0]
        assert set(top) >= {"created_at", "status", "decision", "score", "scores"}
        assert top["status"] == "accepted" and "current" in top["scores"]
        s = client.get("/learning").json()
        assert s["state"]["mode"] == "normal" and s["last_evaluation"]["id"] == top["id"]
        assert s["eval_every_days"] == 7 and "avoid_words" in s
        assert client.put("/config", json={"learning_eval_days": 91}).status_code == 422
        assert client.put("/config", json={"learning_eval_days": 14}).json()["learning_eval_days"] == 14
        with evaluate._RUN_LOCK:
            assert client.post("/learning/evaluate", json={}).status_code == 409
    ev = [e for e in rt.audit.entries(50) if e["event"] == "learning.evaluate"]
    assert ev and set(ev[0]["detail"]) >= {"persona", "status", "scores", "holdout"}
    assert not any(t in str(e["detail"]) for e in ev for t in CASUAL)  # never user text


# ------------------------------------------------------------------ generation robustness
def test_generation_survives_corrupt_or_missing_state(rt):
    _graph(rt, CASUAL)
    rt.db.set_learning_state("Default", "pinned", 9999)  # snapshot does not exist
    learning_state.invalidate()
    assert run_generation("tell sam the meeting moved to three", runtime=rt)["constraints"]["casing"] == "lower"

    snap = _snapshot(rt, {"casing": "lower"})
    with rt.db.connect() as c:
        c.execute("UPDATE constraint_snapshots SET constraints_json = 'enc1:not-a-ciphertext' WHERE id = ?", (snap,))
    rt.db.set_learning_state("Default", "pinned", snap)
    learning_state.invalidate()
    assert run_generation("tell sam the meeting moved to three", runtime=rt)["text"]

    with rt.db.connect() as c:
        c.execute("UPDATE learning_state SET mode = 'nonsense'")
    learning_state.invalidate()
    assert run_generation("tell sam the meeting moved to three", runtime=rt)["text"]

    def broken(persona):
        raise sqlite3.OperationalError("no such table")

    rt.db.get_learning_state = broken
    learning_state.invalidate()
    assert run_generation("tell sam the meeting moved to three", runtime=rt)["constraints"]["casing"] == "lower"


def test_state_is_cached_per_persona(rt, monkeypatch):
    _graph(rt, CASUAL)
    calls = []
    real = rt.db.get_learning_state
    monkeypatch.setattr(rt.db, "get_learning_state", lambda p: calls.append(p) or real(p))
    learning_state.invalidate()
    for _ in range(3):
        run_generation("tell sam the meeting moved to three", runtime=rt)
    assert calls == ["Default"]


# ------------------------------------------------------------------ privacy
def test_wipe_and_persona_delete_clear_evaluations(rt):
    _graph(rt, CASUAL)
    _samples(rt)
    with api_client(rt) as client:
        assert client.post("/personas", json={"name": "Coach", "description": "upbeat"}).status_code in (200, 201)
        snap = _snapshot(rt, {"casing": "lower"}, persona="Coach")
        rt.db.set_learning_state("Coach", "pinned", snap)
        _snapshot(rt, {"casing": "lower"})
        assert client.delete("/personas/Coach").status_code == 200
        assert rt.db.list_snapshots("Coach") == [] and rt.db.get_learning_state("Coach") is None
        assert rt.db.list_snapshots("Default")  # other personas keep theirs
        rt.db.set_learning_state("Default", "corrections_paused")
        out = client.post("/data/wipe", json={"confirm": "delete everything"}).json()["deleted"]
        assert out["constraint_snapshots"] == 1
    assert rt.db.list_snapshots() == [] and rt.db.get_learning_state("Default") is None
    assert learning_state.mode(rt.db, "Default") == ("normal", None)


def test_retention_purge_keeps_evaluations(rt):
    _snapshot(rt, {"casing": "lower"})
    rt.db.purge_older_than((datetime.now(UTC) + timedelta(days=1)).isoformat())
    assert len(rt.db.list_snapshots()) == 1


# ------------------------------------------------------------------ draft outcomes
def test_outcome_endpoint_validation_and_precedence(rt):
    d = rt.db.insert_draft(persona="Default", intent="x", text="hello there, see you soon")
    with api_client(rt) as client:
        assert client.post(f"/drafts/{d['draft_id']}/outcome", json={"outcome": "loved"}).status_code == 422
        assert client.post(f"/drafts/{d['draft_id']}/outcome", json={}).status_code == 422
        assert client.post("/drafts/nope/outcome", json={"outcome": "accepted"}).status_code == 404
        r = client.post(f"/drafts/{d['draft_id']}/outcome", json={"outcome": "accepted"})
        assert r.status_code == 200 and r.json() == {"draft_id": d["draft_id"], "outcome": "accepted"}
        assert client.get("/drafts").json()[0]["outcome"] == "accepted"
        assert client.post(f"/drafts/{d['draft_id']}/outcome", json={"outcome": "edited"}).json()["outcome"] == "edited"
        # a late "accepted" never undoes "edited"
        assert client.post(f"/drafts/{d['draft_id']}/outcome", json={"outcome": "accepted"}).json()["outcome"] == "edited"
    with pytest.raises(ValueError):
        rt.db.set_draft_outcome(d["draft_id"], "loved")


def test_edit_learning_marks_the_draft_edited(rt):
    from .test_learning import DRAFT, SENT

    d = rt.db.insert_draft(persona="Default", intent="x", text=DRAFT)
    rt.db.set_draft_outcome(d["draft_id"], "accepted")  # Rust: injected
    ingest_typed(rt, SENT, SLACK, after_inject=True)
    assert rt.db.get_draft(d["draft_id"])["outcome"] == "edited"
    same = rt.db.insert_draft(persona="Default", intent="y", text="totally fine to send exactly as written by you, thanks")
    ingest_typed(rt, same["text"], SLACK, after_inject=True)
    assert rt.db.get_draft(same["draft_id"])["outcome"] == "accepted"  # sent unchanged


def test_hotkey_again_on_a_draft_marks_it_regenerated(rt):
    first = run_generation("tell sam the meeting moved to three and ask for the notes", runtime=rt)
    second = run_generation(first["text"], runtime=rt)  # the injected draft, selected and re-run
    assert rt.db.get_draft(first["draft_id"])["outcome"] == "regenerated"
    assert rt.db.get_draft(second["draft_id"])["outcome"] is None
    run_generation("something else entirely about lunch plans tomorrow", runtime=rt)
    assert rt.db.get_draft(second["draft_id"])["outcome"] is None
    assert outcomes.note_regeneration(rt.db, "hi", None) is None


def test_migration_adds_outcome_to_an_old_database(tmp_path):
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE drafts (draft_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, persona TEXT NOT NULL,"
        " intent TEXT NOT NULL, text TEXT NOT NULL, latency_ms INTEGER NOT NULL DEFAULT 0, constraints_json TEXT,"
        " score_json TEXT, sha256 TEXT NOT NULL);"
        "INSERT INTO drafts VALUES ('d1', '2026-01-01T00:00:00.000+00:00', 'Default', 'hi', 'hello there friend',"
        " 5, NULL, NULL, 'abc');"
    )
    conn.commit()
    conn.close()
    db = Database(path)
    cols = {r[1] for r in db._conn().execute("PRAGMA table_info(drafts)")}
    assert "outcome" in cols
    assert db.get_draft("d1")["outcome"] is None and db.get_draft("d1")["text"] == "hello there friend"
    assert db.set_draft_outcome("d1", "accepted") == "accepted"
    db.close()
    again = Database(path)  # idempotent
    assert again.get_draft("d1")["outcome"] == "accepted"
    assert again.list_snapshots() == []


# ------------------------------------------------------------------ outcome-weighted few-shots
STATEMENTS = [
    "the launch plan is ready and the team agreed on it.",
    "our budget review went well and nothing big changed today.",
    "the new office opens next month and everyone seems happy.",
]
QUESTIONS = [
    "could you share the final numbers with the whole team today?",
    "would you mind moving our weekly sync to the afternoon slot?",
    "can we keep the demo short and skip the long intro part?",
]
KEPT = [
    "could you send the updated slides to the design team today?",
    "would you mind checking the contract before the call this week?",
]


def test_kept_draft_structures_boost_matching_examples(rt):
    examples.invalidate()
    for t in STATEMENTS + QUESTIONS:
        rt.db.insert_sample(text=t, source="manual", persona="Default", provenance={"source": "manual"})
    intent = "the plan is ready and we agreed"
    before = select_examples(rt.db, "Default", intent, {})
    assert before[0] in STATEMENTS
    drafts = [rt.db.insert_draft(persona="Default", intent="q", text=t) for t in KEPT]
    after_unlabelled = select_examples(rt.db, "Default", intent, {})
    assert after_unlabelled == before  # drafts alone change nothing; only kept ones count
    for d in drafts:
        rt.db.set_draft_outcome(d["draft_id"], "accepted")
    kept = examples.kept_signatures(rt.db, "Default")
    assert kept and all("|" in s for s in kept)  # signatures only, no words
    after = select_examples(rt.db, "Default", intent, {})
    assert after[0] in QUESTIONS
    assert not set(after) & set(KEPT)  # examples are the user's samples, never drafts
    assert all(any(e in s for s in STATEMENTS + QUESTIONS) for e in after)
    assert select_examples(rt.db, "Default", intent, {}) == after  # deterministic (memo hit)
    # a discarded draft does not boost
    for d in drafts:
        rt.db.set_draft_outcome(d["draft_id"], "discarded")
    assert select_examples(rt.db, "Default", intent, {}) == before
