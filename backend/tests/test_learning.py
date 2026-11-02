"""Edit learning (backend/learning/edits.py): what the user changes in a draft corrects the rules.

A synthetic stream of edits where the user always deletes em-dashes and one pet word, and
shortens every sentence, must move the persona's constraints that way, within the caps, only
after MIN_EDITS edits, without ever touching the style graph or the samples.
"""
from __future__ import annotations

from backend.graph.store import InMemoryGraphStore
from backend.ingestion.extract import extract
from backend.ingestion.sources import (
    AFTER_INJECT_LEARNED,
    AFTER_INJECT_REASON,
    ingest_typed,
)
from backend.langgraph.prompts import build_system_parts
from backend.learning import edits
from backend.security import crypto

from .conftest import HUMAN_SAMPLES

SLACK = "com.tinyspeck.slackmacgap"

TOPICS = [("sam", "meeting", "three", "slides"), ("ana", "review", "noon", "notes"),
          ("lee", "demo", "monday", "deck"), ("kim", "launch", "friday", "plan"),
          ("raj", "standup", "ten", "agenda"), ("mia", "offsite", "june", "budget")]
DRAFT_T = ("hey {0} — i'm delighted to say the {1} moved to {2} — it was a scheduling clash on our "
           "side and we wanted to give everyone enough time to prepare properly. delighted to send the "
           "{3} tonight — let me know if that works for you.")
SENT_T = ("hey {0}, the {1} moved to {2}. it was a scheduling clash on our side. "
          "i'll send the {3} tonight. let me know if that works for you.")
DRAFT, SENT = DRAFT_T.format(*TOPICS[0]), SENT_T.format(*TOPICS[0])


def _learn(rt, n: int, persona: str = "Default", draft: str | None = None, sent: str | None = None) -> list[dict]:
    """``n`` edits. By default each one is about a different topic (as real drafts are), and the
    user always removes the dashes and "delighted" and splits the long sentences."""
    out = []
    for i in range(n):
        topic = TOPICS[i % len(TOPICS)]
        d = rt.db.insert_draft(persona=persona, intent=f"intent {i}", text=draft or DRAFT_T.format(*topic))
        sent_i = sent if sent is not None else SENT_T.format(*topic)
        out.append(ingest_typed(rt, sent_i, SLACK, after_inject=True))
        assert rt.db.list_draft_edits(1)[0]["nearest_draft_id"] == d["draft_id"]
    return out


def _seed(rt, persona: str = "Default") -> None:
    for t in HUMAN_SAMPLES:
        rt.store.write_extraction(extract(t), persona)


def test_edit_features_measure_what_the_user_changed():
    f = edits.edit_features(DRAFT, SENT)
    assert f["scalars"]["len"] < 0  # shorter sentences
    assert f["punct"]["—"] < 0  # dashes removed
    assert f["words"]["delighted"] == 1.0 and f["words"]["meeting"] == 0.0
    same = edits.edit_features(DRAFT, DRAFT)  # sent unchanged: every delta is zero
    assert all(v == 0 for v in same["scalars"].values()) and all(v == 0 for v in same["punct"].values())
    assert set(same["words"].values()) == {0.0}


def test_match_draft_picks_the_edited_draft(runtime):
    other = runtime.db.insert_draft(persona="Friend", intent="x", text="totally different words about lunch plans today")
    mine = runtime.db.insert_draft(persona="Boss", intent="y", text=DRAFT)
    match = edits.match_draft(runtime.db, SENT)
    assert match is not None and match[0]["draft_id"] == mine["draft_id"] and match[0]["persona"] == "Boss"
    assert match[1] >= edits.MIN_MATCH and other["draft_id"]
    assert edits.match_draft(runtime.db, "nothing in common with any draft whatsoever, ok?") is None


def test_edit_stream_moves_constraints_within_caps(runtime):
    _seed(runtime)
    base = runtime.store.get_constraints("Default")
    assert base["punctuation_rates"]["—"] > 0

    r = _learn(runtime, edits.MIN_EDITS - 1)
    assert r[-1]["reasons"] == [AFTER_INJECT_LEARNED]
    early = edits.corrected(runtime.db, "Default", dict(base))
    assert early["avoid_words"] == [] and early["avg_sentence_len"] == base["avg_sentence_len"]  # not enough edits

    _learn(runtime, 3)
    c = edits.corrected(runtime.db, "Default", dict(base))
    assert c["edits_learned"] == edits.MIN_EDITS + 2
    assert "delighted" in c["avoid_words"]
    assert not {w for t in TOPICS for w in t} & set(c["avoid_words"])  # content words come and go
    assert c["punctuation_rates"]["—"] == 0 and "—" in c["avoid_punctuation"]
    assert c["avg_sentence_len"] < base["avg_sentence_len"]
    assert c["avg_sentence_len"] >= base["avg_sentence_len"] * (1 - edits.MAX_SHIFT) - 1e-9
    assert c["sentence_len_p90"] >= c["avg_sentence_len"]

    stable, _ = build_system_parts(c, "Default")
    assert "keeps deleting from your drafts" in stable and '"delighted"' in stable
    assert "em-dashes (—)" in stable.split("Never use", 1)[1]


def test_learning_never_touches_graph_or_samples(runtime):
    _seed(runtime)
    words, stats = dict(runtime.store.words), runtime.store.stats()
    _learn(runtime, 4)
    assert dict(runtime.store.words) == words and runtime.store.stats() == stats
    assert runtime.db.count_samples() == 0


def test_unrelated_text_after_inject_learns_nothing(runtime):
    runtime.db.insert_draft(persona="Default", intent="x", text=DRAFT)
    r = ingest_typed(runtime, "completely unrelated reply about the weekend trip, see you there!", SLACK,
                     after_inject=True)
    assert r["reasons"] == [AFTER_INJECT_REASON]
    assert runtime.db.list_draft_edits()[0]["nearest_draft_id"] is None
    assert runtime.db.get_corrections("Default") == []


def test_corrections_are_per_persona_and_bounded(runtime):
    _seed(runtime, "Boss")
    _learn(runtime, 5, persona="Boss")
    assert runtime.db.get_corrections("Default") == []
    base = runtime.store.get_constraints("Boss")
    # an extreme rewrite (same words, every sentence cut to two) moves length by at most MAX_SHIFT
    toks = DRAFT.replace("—", "").split()
    choppy = ". ".join(" ".join(toks[i : i + 2]) for i in range(0, len(toks), 2)) + "."
    _learn(runtime, 10, persona="Boss", draft=DRAFT, sent=choppy)
    c = edits.corrected(runtime.db, "Boss", dict(base))
    assert c["avg_sentence_len"] >= base["avg_sentence_len"] * (1 - edits.MAX_SHIFT) - 1e-9
    assert 0.0 <= c["contractions_rate"] <= 1.0
    assert len(c["avoid_words"]) <= edits.MAX_AVOID_WORDS


def test_unchanged_drafts_pull_corrections_back(runtime):
    """Sending drafts as they are is evidence the rules are right: the smoothed deltas decay."""
    _seed(runtime)
    _learn(runtime, 4)
    before = {(r["feature"], r["key"]): r["value"] for r in runtime.db.get_corrections("Default")}
    _learn(runtime, 6, draft=DRAFT, sent=DRAFT)
    after = {(r["feature"], r["key"]): r["value"] for r in runtime.db.get_corrections("Default")}
    assert abs(after[("len", "")]) < abs(before[("len", "")])
    assert abs(after[("punct", "—")]) < abs(before[("punct", "—")])


def test_corrections_encrypted_at_rest(runtime):
    _learn(runtime, 1)
    with runtime.db.connect() as c:
        rows = c.execute("SELECT key, text FROM corrections WHERE feature = 'word'").fetchall()
    assert rows and all(crypto.is_index(r["key"]) for r in rows)
    assert all(crypto.is_encrypted(r["text"]) for r in rows)
    assert not any("delighted" in (r["key"] + r["text"]) for r in rows)


def test_generate_uses_corrections_and_learning_endpoint(client, runtime):
    _seed(runtime)
    _learn(runtime, edits.MIN_EDITS)
    d = client.post("/generate", json={"intent": "tell sam the meeting moved to three"}).json()
    assert "delighted" in d["constraints"]["avoid_words"]
    s = client.get("/learning").json()
    assert s["persona"] == "Default" and s["edits_learned"] == edits.MIN_EDITS
    assert "delighted" in s["avoid_words"] and s["punctuation"]["—"]["value"] < 0


def test_persona_delete_and_wipe_clear_corrections(client, runtime):
    assert client.post("/personas", json={"name": "Coach", "description": "upbeat"}).status_code in (200, 201)
    _learn(runtime, 2, persona="Coach")
    assert runtime.db.get_corrections("Coach")
    assert client.delete("/personas/Coach").status_code == 200
    assert runtime.db.get_corrections("Coach") == []
    _learn(runtime, 1)
    assert client.post("/data/wipe", json={"confirm": "delete everything"}).status_code == 200
    assert runtime.db.get_corrections("Default") == []


def test_in_memory_store_type_unchanged():
    assert InMemoryGraphStore().get_constraints("Default").keys() == edits.apply_corrections(
        InMemoryGraphStore().get_constraints("Default"), []
    ).keys()
