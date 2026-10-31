from backend.graph.constraints import empty_constraints
from backend.langgraph.generate_graph import (
    normalize_intent,
    postprocess_text,
    run_generation,
)
from backend.langgraph.ingest_graph import run_ingest
from backend.langgraph.prompts import build_system_prompt

from .conftest import HUMAN_SAMPLES


def test_normalize_intent():
    assert normalize_intent("- tell sam x\n\n* ask   y\n1. z\n• w\n[ ] v") == "tell sam x\nask y\nz\nw\nv"


def test_postprocess():
    assert postprocess_text('Here\'s the rewritten message:\n\n"Hi Sam, see you at 3."') == "Hi Sam, see you at 3."
    assert postprocess_text("Sure! Here is your draft:\nhey sam") == "hey sam"
    assert postprocess_text("Draft: ok see you") == "ok see you"
    assert postprocess_text("```\nhello\n```") == "hello"
    assert postprocess_text("hey there\n\nLet me know if you want changes.") == "hey there"
    assert postprocess_text('"a" and "b"') == '"a" and "b"'
    assert postprocess_text("Here we go again, team.") == "Here we go again, team."


def test_prompt_empty_and_full():
    p = build_system_prompt(empty_constraints(), "Boss", "Slack")
    assert "no writing samples" in p and "Boss" in p and "Slack" in p and "ONLY the final" in p
    c = dict(empty_constraints(), sample_count=5, avg_sentence_len=8.0, sentence_len_p90=14,
             casing="lower", contractions_rate=0.9, punctuation_rates={"—": 0.5, "!": 0.0},
             top_openers=["so"], top_transitions=["anyway"], lexicon=["ship"],
             top_bigrams=[["sounds", "good", 3.0]])
    p = build_system_prompt(c, "Friend", None)
    for frag in ("HARD STYLE RULES", "average 8.0 words", "±2", "all lowercase", "Contraction rate 0.90",
                 "em-dashes (—): often", "Never use exclamation marks", '"so"', '"anyway"', "ship",
                 '"sounds good"', "Never invent facts"):
        assert frag in p, frag


def test_generation_roundtrip(runtime):
    for t in HUMAN_SAMPLES:
        assert run_ingest(t, "manual", "Friend", {}, runtime=runtime)["accepted"] == 1
    words_before = dict(runtime.store.words)
    out = run_generation("- tell sam meeting moved to 3\n- ask for slides", "Friend", "Slack", runtime=runtime)
    assert set(out) == {"draft_id", "text", "persona", "latency_ms", "constraints", "model"}
    assert out["persona"] == "Friend" and out["constraints"]["sample_count"] == 3
    assert "sam" in out["text"].lower() and "slides" in out["text"]
    assert out["text"] == out["text"].lower()  # mock honoured the lowercase casing rule
    assert not out["text"].startswith(("-", "Here"))
    d = runtime.db.get_draft(out["draft_id"])
    assert d["text"] == out["text"] and d["constraints"] == out["constraints"]
    # generated text never reaches the graph
    assert dict(runtime.store.words) == words_before
    system, user = runtime.llm.calls[-1]
    assert "HARD STYLE RULES" in system and "<intent>" in user and "- tell" not in user


def test_generation_uses_active_persona(runtime):
    runtime.db.set_config(active_persona="Investor")
    assert run_generation("update on q3", runtime=runtime)["persona"] == "Investor"


def test_generation_survives_graph_failure(runtime):
    class Broken:
        kind = "neo4j"

        def get_constraints(self, persona, k=20):
            raise ConnectionError("neo4j down")

    runtime.store = Broken()
    runtime._graphs.clear()
    out = run_generation("- ping ops", "Default", None, runtime=runtime)
    assert out["constraints"] == empty_constraints() and out["text"]


def test_llm_failure_raises_instead_of_mock_fallback(runtime):
    import pytest

    from backend.langgraph.generate_graph import LLMFailedError, LLMTimeoutError

    class BadLLM:
        name = "anthropic"

        def __init__(self, exc):
            self.exc = exc

        def complete(self, system, user):
            raise self.exc

    for exc, err in ((TimeoutError("slow"), LLMTimeoutError), (RuntimeError("500 overloaded"), LLMFailedError)):
        runtime.llm = BadLLM(exc)
        runtime._graphs.clear()
        with pytest.raises(err):
            run_generation("- say thanks", runtime=runtime)
    assert runtime.db.list_drafts() == []  # nothing recorded, nothing to inject


def test_score_hook(runtime):
    calls = []

    def fake_score(text, baseline, persona=None):
        calls.append((text, baseline, persona))
        return {"score": 77.0, "distance": 0.23, "components": {}}

    runtime.scorer = fake_score
    run_ingest(HUMAN_SAMPLES[0], "manual", "Default", {}, runtime=runtime)
    out = run_generation("- hi", "Default", runtime=runtime)
    assert calls and calls[0][1] == [HUMAN_SAMPLES[0]] and calls[0][2] == "Default"
    assert runtime.db.get_draft(out["draft_id"])["score"]["score"] == 77.0


def test_score_skipped_without_scorer(runtime, monkeypatch):
    import backend.runtime as rtmod

    runtime.scorer = None  # auto-discover → style_distance.score may not exist yet
    monkeypatch.setattr(rtmod, "load_scorer", lambda: None)
    out = run_generation("- hi", runtime=runtime)
    assert runtime.db.get_draft(out["draft_id"])["score"] is None
