"""Style Distance Score (backend/evaluation) — feature-only, offline, deterministic."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.evaluation import embeddings
from backend.evaluation.calibrate import run as run_calibration
from backend.evaluation.features import (
    FUNCTION_WORDS,
    build_baseline,
    extract_features,
    function_word_distance,
    lexical_distance,
    punctuation_distance,
    sentence_length_distance,
)
from backend.evaluation.style_distance import WEIGHTS, combine, score, score_draft

FIXTURE = Path(__file__).parent / "fixtures" / "eval" / "voices.json"
VOICES = {k: v for k, v in json.loads(FIXTURE.read_text()).items() if not k.startswith("_")}
COMPONENTS = {"sentence_length", "punctuation", "lexical", "function_words", "embedding"}


def _check_shape(r: dict) -> None:
    assert set(r) >= {"score", "distance", "components", "confidence"}
    assert set(r["components"]) == COMPONENTS
    assert 0.0 <= r["distance"] <= 1.0
    assert 0.0 <= r["score"] <= 100.0
    assert r["score"] == round(100 * (1 - r["distance"]), 1)
    for k, v in r["components"].items():
        if v is not None:
            assert 0.0 <= v <= 1.0, k
    assert r["confidence"] in ("low", "medium", "high")
    json.dumps(r)  # must be JSON-serializable for drafts.score_json


def test_embeddings_disabled_in_tests():
    assert embeddings.enabled() is False
    assert embeddings.embedding_distance("hello there", ["hi"]) is None


def test_function_word_list_size():
    assert 100 <= len(FUNCTION_WORDS) <= 150
    assert len(set(FUNCTION_WORDS)) == len(FUNCTION_WORDS)


def test_features_basic():
    f = extract_features("hey — i don't think so... can't make it! ok?")
    assert f.n_words == 9
    rates = f.punctuation_rates()
    assert rates["—"] > 0 and rates["…"] > 0 and rates["!"] > 0 and rates["?"] > 0
    assert rates["."] == 0  # the ellipsis is not counted as periods
    assert f.n_contractions == 2
    assert f.casing() == "lower"
    assert extract_features("Hello there. I think so 🎉").punctuation_rates()["emoji"] > 0


@pytest.mark.parametrize("voice", sorted(VOICES))
def test_same_author_beats_different_author(voice):
    base = VOICES[voice]["baseline"]
    others = [o for o in VOICES if o != voice]
    same = [score(t, base, use_embeddings=False)["score"] for t in VOICES[voice]["heldout"]]
    diff = [
        score(t, base, use_embeddings=False)["score"]
        for o in others
        for t in VOICES[o]["heldout"] + VOICES[o]["heldout_cross_topic"]
    ]
    # every held-out same-author text beats every other-voice text, by a real margin on average
    assert min(same) > max(diff)
    assert sum(same) / len(same) - sum(diff) / len(diff) >= 15


@pytest.mark.parametrize("voice", sorted(VOICES))
def test_style_beats_topic(voice):
    """Same voice on the other voice's topic still scores above the other voice."""
    base = VOICES[voice]["baseline"]
    cross = [score(t, base, use_embeddings=False)["score"] for t in VOICES[voice]["heldout_cross_topic"]]
    other = next(o for o in VOICES if o != voice)
    diff = [score(t, base, use_embeddings=False)["score"] for t in VOICES[other]["heldout"]]
    assert sum(cross) / len(cross) > sum(diff) / len(diff)


def test_component_distances_separate_voices():
    slack, formal = VOICES["slack"], VOICES["formal"]
    base = build_baseline(slack["baseline"])
    same = extract_features(" ".join(slack["heldout"]))
    other = extract_features(" ".join(formal["heldout"]))
    for fn in (sentence_length_distance, punctuation_distance, lexical_distance, function_word_distance):
        assert fn(same, base) < fn(other, base), fn.__name__


def test_deterministic_feature_only():
    base = VOICES["formal"]["baseline"]
    text = VOICES["slack"]["heldout"][0]
    first = score(text, base, use_embeddings=False)
    for _ in range(3):
        assert score(text, base, use_embeddings=False) == first
    # baseline order must not matter for the stylometric features
    assert score(text, list(reversed(base)), use_embeddings=False)["score"] == first["score"]
    _check_shape(first)
    assert first["components"]["embedding"] is None


@pytest.mark.parametrize("voice", sorted(VOICES))
def test_identical_text_scores_100(voice):
    t = VOICES[voice]["baseline"][0]
    r = score(t, [t], use_embeddings=False)
    _check_shape(r)
    assert r["score"] >= 99.0
    # the whole baseline scored against itself is also ~100
    whole = VOICES[voice]["baseline"]
    assert score("\n".join(whole), whole, use_embeddings=False)["score"] >= 90.0


@pytest.mark.parametrize("text", ["", "   ", "\n\n"])
def test_empty_text(text):
    r = score(text, VOICES["slack"]["baseline"], use_embeddings=False)
    _check_shape(r)
    assert r["score"] == 0.0 and r["confidence"] == "low"


def test_empty_baseline():
    r = score("hello there", [], use_embeddings=False)
    _check_shape(r)
    assert r["score"] == 0.0 and r["confidence"] == "low"
    _check_shape(score("hello there", ["", "  "], use_embeddings=False))


@pytest.mark.parametrize("text", ["ok", "k.", "👍", "yes!", "...", "Thanks"])
def test_very_short_text(text):
    r = score(text, VOICES["slack"]["baseline"], use_embeddings=False)
    _check_shape(r)
    assert r["confidence"] == "low"


def test_tiny_baseline_still_scores_low_confidence():
    base = VOICES["slack"]["baseline"][:2]
    r = score(VOICES["slack"]["heldout"][0], base, use_embeddings=False)
    _check_shape(r)
    assert r["confidence"] == "low"
    assert r["score"] > score(VOICES["formal"]["heldout"][0], base, use_embeddings=False)["score"]


def test_confidence_levels():
    base = VOICES["formal"]["baseline"]
    long_text = " ".join(VOICES["formal"]["heldout"])
    assert score(long_text, base * 2, use_embeddings=False)["confidence"] == "high"
    assert score(long_text, base[:8], use_embeddings=False)["confidence"] == "medium"


def test_weights_and_renormalization():
    assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9
    comps = {"sentence_length": 0.2, "punctuation": 0.2, "lexical": 0.2, "function_words": 0.2, "embedding": None}
    assert combine(comps) == pytest.approx(0.2)
    comps["embedding"] = 0.8
    assert combine(comps) == pytest.approx(0.2 * 0.75 + 0.8 * 0.25)


def test_embedding_none_when_model_fails(monkeypatch):
    monkeypatch.setenv("POLYTONE_EMBEDDINGS", "qwen")
    monkeypatch.setattr(embeddings, "_model", None)
    monkeypatch.setattr(embeddings, "_load_failed", True)  # simulate failed load
    r = score(VOICES["slack"]["heldout"][0], VOICES["slack"]["baseline"])
    _check_shape(r)
    assert r["components"]["embedding"] is None


def test_calibration_report_runs():
    rep = run_calibration(use_embeddings=False)
    for v in rep["voices"].values():
        s = v["summary"]
        assert s["gap"] > 15 and s["identical"] >= 99.0


def test_score_draft_persists(db):
    db.insert_sample(text=VOICES["slack"]["baseline"][0], source="test", persona="work")
    for t in VOICES["slack"]["baseline"][1:]:
        db.insert_sample(text=t, source="test", persona="work")
    d = db.insert_draft(persona="work", intent="x", text=VOICES["slack"]["heldout"][0], latency_ms=1)
    r = score_draft(d["draft_id"], db)
    _check_shape(r)
    assert db.get_draft(d["draft_id"])["score"] == r
    assert score_draft("nope", db) is None


def test_runtime_scorer_hook_is_ours():
    """generate_graph discovers us via runtime.load_scorer and calls scorer(text, baseline)."""
    from backend.runtime import load_scorer

    fn = load_scorer()
    assert fn is score
    _check_shape(fn("hey whats up", VOICES["slack"]["baseline"]))
