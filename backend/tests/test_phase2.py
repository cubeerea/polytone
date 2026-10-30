"""Phase 2: extractor, firewall hardening, blended retrieval, few-shot prompt, LLM errors."""
from __future__ import annotations

import copy
import json
import sys
import types
from pathlib import Path

import pytest

from backend.graph.constraints import BLEND_PRIOR, blend_weight
from backend.graph.store import InMemoryGraphStore
from backend.ingestion.extract import extract, signature
from backend.ingestion.firewall import check
from backend.ingestion.text import clean_message, split_sentences, tokenize
from backend.ingestion.types import Extraction
from backend.langgraph.examples import select_examples
from backend.langgraph.generate_graph import run_generation
from backend.langgraph.ingest_graph import run_ingest
from backend.langgraph.prompts import build_system_prompt, describe_structure

CORPUS = json.loads((Path(__file__).parent / "fixtures" / "corpus.json").read_text())


def _load_corpus(rt):
    for persona in ("Boss", "Friend"):
        for t in CORPUS[persona]:
            r = run_ingest(t, "manual", persona, {}, runtime=rt)
            assert r["accepted"] == 1, (t, r)


# =========================================================================== extraction
def test_sentence_splitting_edge_cases():
    assert split_sentences("Hi Dr. Smith, see https://example.com/a.b?x=1. Then email j.doe@corp.io.") == [
        "Hi Dr. Smith, see https://example.com/a.b?x=1.", "Then email j.doe@corp.io."]
    assert split_sentences("Meet at 3 p.m. tomorrow, e.g. in room 4. Thanks!") == [
        "Meet at 3 p.m. tomorrow, e.g. in room 4.", "Thanks!"]
    assert split_sentences("lol ok 😂 that's wild\non it\nwhat time?? 9 works") == [
        "lol ok 😂", "that's wild", "on it", "what time??", "9 works"]
    assert split_sentences("Plan:\n- ship it\n* test it\n1. celebrate") == ["Plan:", "ship it", "test it", "celebrate"]
    # hard-wrapped email lines are joined, version numbers and decimals are not split
    wrapped = "I looked at the numbers again and I think we should hold off until\nnext week. v2.1 costs 3.5x more."
    assert split_sentences(wrapped) == [
        "I looked at the numbers again and I think we should hold off until next week.", "v2.1 costs 3.5x more."]
    assert split_sentences("") == [] and split_sentences("👍") == []


def test_clean_message_strips_replies_and_signatures():
    assert clean_message("sounds good!\n\nOn Tue, Oct 13, 2026 at 9:00 AM Sam <s@x.com> wrote:\n> old") == "sounds good!"
    assert clean_message("ok\nOn Tue, Oct 13, 2026 at 9:00 AM Sam Lee\n<s@x.com> wrote:\n> old") == "ok"
    assert clean_message("will do.\n\n-- \nSam Lee\nCEO") == "will do."
    assert clean_message("on my way\n\nSent from my iPhone") == "on my way"
    assert clean_message("see below\n\n-----Original Message-----\nFrom: a\nhi") == "see below"
    assert clean_message("agreed.\n\nFrom: Dana\nSent: Monday\nTo: me\n\nold text") == "agreed."
    assert clean_message("> quoted line\nmy reply here") == "my reply here"
    assert clean_message("Get Outlook for iOS") == ""
    # extraction only ever sees the user's own words
    ex = extract("thanks, will do.\n\nOn Mon, Jan 5, 2026 at 1:00 PM Bob <b@x.com> wrote:\n> quarterly synergy")
    assert "synergy" not in ex.words and "bob" not in ex.words and ex.sentence_lengths == [3]


def test_tokens_words_and_bigrams():
    ex = extract("I'll send 3 files to https://x.com/a today. Don't wait for me, y'all.")
    assert "i'll" in ex.words and "don't" in ex.words and "y'all" in ex.words
    assert "3" not in ex.words and not any("x.com" in w or w == "https" for w in ex.words)
    assert ex.sentence_lengths == [7, 5]  # numbers and URLs still count for cadence
    assert ("send", "3") not in ex.bigrams and ("3", "files") not in ex.bigrams
    assert ("i'll", "send") in ex.bigrams and ("for", "me") in ex.bigrams
    assert tokenize("I’m at q3 e.g. now") == ["i'm", "at", "q3", "e.g.", "now"]


def test_signatures_capture_cadence():
    assert signature("So we pushed the release, then the team took a break — mostly.") == "M|2c|,—|conj|."
    assert signature("Can you check?") == "XS|1c|_|aux|?"
    assert signature("omg yes!!") == "XS|1c|_|word|!!"
    assert signature("haha amazing 😂") == "XS|1c|_|disc|e"
    assert signature("ship it", bullet=True) == "XS|1c|_|list|_"
    assert signature("Honestly, I think it's fine because we tested it, and they agreed.").split("|")[1] == "3c"
    # meaning-free: same shape, different words → same signature
    assert signature("Thanks for the notes, I'll fix it.") == signature("Thanks for the pizza, I'll owe you.")


def test_punctuation_casing_contractions_openers_transitions():
    ex = extract(
        "honestly it's fine -- no rush!! (seriously) i'm ok... anyway, that said, we're good :) "
        "So that's that. We said so too."
    )
    p = ex.punctuation
    assert p["—"] == 1 and p["!"] == 1 and p["!!"] == 1 and p["("] == 1 and p["…"] == 1
    assert p["emoji"] == 1 and p[","] == 2 and p["."] == 2
    assert ex.transitions == {"honestly": 1, "anyway": 1, "that said": 1, "so": 1}  # mid-sentence "so" ignored
    assert ex.openers["honestly"] == 1 and ex.opener_ngrams["honestly it's"] == 1
    assert ex.lower_starts >= 1 and ex.upper_starts == 2 and ex.lower_i == 1 and ex.upper_i == 0  # "i'm"
    assert ex.contractions == 4 and ex.expansions == 0
    # function / opener / marker words link to structures; content words do not
    linked = {w for w, _ in ex.word_structures}
    assert {"honestly", "anyway", "that", "so"} <= linked and "rush" not in linked


def test_extract_deterministic_and_roundtrip():
    t = CORPUS["Boss"][1]
    a, b = extract(t), extract(t)
    assert a == b and Extraction.from_any(a.to_dict()) == a
    assert "dana@acme.com" not in json.dumps(a.to_dict()) and "offsite" not in a.words


# =========================================================================== firewall
def test_firewall_provenance_per_source(db):
    t = "I'll get the numbers over to you tomorrow."
    assert check(t, "gmail", {}, db)[0] is False
    assert "sent" in check(t, "gmail", {"direction": "outbound"}, db)[1]
    assert check(t, "gmail", {"label": "SENT"}, db) == (True, None)
    assert check(t, "gmail", {"labels": ["INBOX", "SENT"]}, db) == (True, None)
    assert check(t, "gmail", {"query": "in:sent from:me"}, db) == (True, None)
    assert check(t, "gmail", {"from_me": True}, db) == (True, None)
    assert "from_me=False" in check(t, "gmail", {"from_me": False, "label": "SENT"}, db)[1]
    assert "From: the user" in check(t, "watcher", {"kind": "mbox", "file": "a.mbox"}, db)[1]
    assert "From: the user" in check(t, "watcher", {"path": "/x/inbox.MBOX"}, db)[1]
    assert check(t, "watcher", {"kind": "mbox", "from_me": True}, db) == (True, None)
    assert check(t, "watcher", {"kind": "txt", "file": "notes.txt"}, db) == (True, None)
    assert "raw transcript" in check(t, "wispr", {}, db)[1]
    assert "formatted" in check(t, "wispr", {"raw_transcript": True, "formatted": True}, db)[1]
    assert check(t, "wispr", {"raw_transcript": True}, db) == (True, None)
    assert check(t, "manual", {}, db) == (True, None)


def test_firewall_length_duplicates_and_drafts(db):
    assert "too short" in check("ok", "manual", {}, db)[1]
    assert "too short" in check("ok cool", "manual", {}, db)[1]
    db.insert_sample(text="this one is already stored.", source="manual", persona="Default")
    assert check("this one is already stored.", "manual", {}, db) == (False, "duplicate sample")
    draft = "Dana, the launch moved to Friday because billing needs another week."
    db.insert_draft(persona="Boss", intent="x", text=draft)
    cases = {
        draft: "matches",
        f"Hi team,\n\n{draft.upper()}\n\nThanks, Sam": "contains a Polytone-generated draft",
        "the launch moved to Friday because billing": "excerpt",
        "Dana, the launch moved to Friday because billing needs another week or so.": "contains",
        "Dana, the launch moved to Friday because billing needs another month.": "near-duplicate",
    }
    for text, frag in cases.items():
        ok, reason = check(text, "manual", {}, db)
        assert not ok and frag in reason and "draft" in reason, (text, reason)
    assert check("the launch party was fun, see you friday", "manual", {}, db) == (True, None)


def test_every_rejection_has_a_reason(runtime):
    texts = ["", "   ", "hi", "> only quoted\n> stuff", "On Mon, Jan 5, 2026 at 1:00 PM Bob <b@x.com> wrote:\n> x"]
    for t in texts:
        r = run_ingest(t, "manual", "Default", {}, runtime=runtime)
        assert r["rejected"] == 1 and r["reasons"] and all(isinstance(x, str) and x for x in r["reasons"]), t


# =========================================================================== retrieval
def test_blend_weight_documented_values():
    assert blend_weight(0, 10) == 0.0 and blend_weight(5, 5) == 1.0
    assert blend_weight(1, 10) == 1 / (1 + BLEND_PRIOR) and blend_weight(3, 10) == 0.5


def test_constraints_differ_between_personas():
    s = InMemoryGraphStore()
    for persona in ("Boss", "Friend"):
        for t in CORPUS[persona]:
            s.write_extraction(extract(t), persona)
    boss, friend = s.get_constraints("Boss"), s.get_constraints("Friend")
    assert boss["sample_count"] == 8 and friend["sample_count"] == 8
    assert boss["casing"] == "sentence" and friend["casing"] == "lower"
    assert boss["avg_sentence_len"] > friend["avg_sentence_len"] + 2
    # absence is evidence: Boss never uses emoji / "!!", Friend never uses em-dashes or colons
    assert "emoji" not in boss["punctuation_rates"] and "!!" not in boss["punctuation_rates"]
    assert friend["punctuation_rates"]["emoji"] > 0.1 and friend["punctuation_rates"]["!!"] > 0.1
    assert boss["punctuation_rates"]["—"] > 0.05 and "—" not in friend["punctuation_rates"]
    assert boss["top_openers"][0] == "quick update on"
    assert "honestly" in friend["top_openers"] and "honestly" not in boss["top_transitions"]
    assert {"customer", "migration"} <= set(boss["lexicon"]) and "u" in friend["lexicon"]
    assert not set(boss["lexicon"]) & set(friend["lexicon"])
    assert not {"the", "and", "get", "time", "thanks"} & set(boss["lexicon"] + friend["lexicon"])
    b_sigs = {x["signature"] for x in boss["structures"][:5]}
    f_sigs = {x["signature"] for x in friend["structures"][:5]}
    assert not b_sigs & f_sigs
    assert ["quick", "update", 4.0] in boss["top_bigrams"]
    # a fresh persona is the user's overall style
    inv = s.get_constraints("Investor")
    assert inv["sample_count"] == 16 and inv == s.get_constraints(None)


def test_blend_shrinks_small_personas_towards_global():
    s = InMemoryGraphStore()
    for t in CORPUS["Friend"]:
        s.write_extraction(extract(t), "Friend")
    s.write_extraction(extract("Please find the revised budget attached for your review."), "Board")
    board = s.get_constraints("Board")
    assert board["sample_count"] == 1
    only = s.get_constraints("Friend")  # 8 lowercase samples dominate the global mix
    assert board["avg_sentence_len"] < 9  # pulled from 9 towards the Friend average
    assert board["avg_sentence_len"] > only["avg_sentence_len"]
    assert board["lexicon"][0] in ("budget", "revised", "review", "attached", "find")  # vocabulary: persona-only
    assert "lol" not in board["lexicon"] and ["the", "revised", 1.0] in board["top_bigrams"]


# =========================================================================== few-shot prompt
def test_examples_from_persona_samples_never_drafts(runtime):
    _load_corpus(runtime)
    c = runtime.store.get_constraints("Friend")
    ex = select_examples(runtime.db, "Friend", "tell jo i can't make it tonight, maybe tomorrow", c)
    assert 2 <= len(ex) <= 3
    friend_texts = " ".join(" ".join(t.split()) for t in CORPUS["Friend"])
    assert all(e in friend_texts for e in ex)
    # a draft that copies a human sentence disqualifies that sentence as an example
    runtime.db.insert_draft(persona="Friend", intent="x", text=ex[0] + " see you soon")
    again = select_examples(runtime.db, "Friend", "tell jo i can't make it tonight, maybe tomorrow", c)
    assert ex[0] not in again and len(again) >= 2
    # Boss examples come from Boss samples and never contain quoted replies or signatures
    boss = select_examples(runtime.db, "Boss", "update dana on the migration status", runtime.store.get_constraints("Boss"))
    assert boss and all("wrote:" not in e and "iPhone" not in e and "Acme Corp" not in e for e in boss)
    # unknown persona with no samples falls back to all samples
    assert select_examples(runtime.db, "Investor", "hi", runtime.store.get_constraints("Investor"))


def test_system_prompt_with_constraints_and_examples(runtime):
    _load_corpus(runtime)
    out = run_generation("- tell jo i can't make it tonight\n- maybe tomorrow?", "Friend", "Slack", runtime=runtime)
    system, _ = runtime.llm.calls[-1]
    assert "HARD STYLE RULES (learned from 8" in system and "Casing: all lowercase" in system
    assert "Never use em-dashes (—)" in system and "emoji: often" in system
    assert system.count("<example>") in (2, 3) and "NOT the content" in system
    assert "Sentence shapes the user uses most" in system
    assert out["text"] == out["text"].lower()
    assert describe_structure("M|2c|,—|conj|.") == (
        "9-15 words, 2 clauses, uses , —, opens with a conjunction (and/but/so), ends with a period")
    assert describe_structure("garbage") == "garbage"
    p = build_system_prompt(runtime.store.get_constraints("Boss"), "Boss", None, ["Quick update: done."])
    assert "<example>Quick update: done.</example>" in p and '"quick update on"' in p


# =========================================================================== LLM errors
class _FailingLLM:
    name = "anthropic"

    def __init__(self, exc):
        self.exc = exc

    def complete(self, system, user):
        raise self.exc


def test_generate_returns_502_and_504(client, runtime):
    import httpx

    runtime.llm = _FailingLLM(RuntimeError("overloaded_error"))
    runtime._graphs.clear()
    r = client.post("/generate", json={"intent": "- tell sam hi"})
    assert r.status_code == 502 and r.json()["error"] == "llm_failed" and "overloaded" in r.json()["detail"]
    req = httpx.Request("POST", "https://api.anthropic.com")
    runtime.llm = _FailingLLM(httpx.ReadTimeout("read timed out", request=req))
    r = client.post("/generate", json={"intent": "- tell sam hi"})
    assert r.status_code == 504 and r.json() == {"error": "llm_timeout", "detail": "ReadTimeout: read timed out"}

    class Empty:
        name = "anthropic"

        def complete(self, system, user):
            return "  "

    runtime.llm = Empty()
    r = client.post("/generate", json={"intent": "- tell sam hi"})
    assert r.status_code == 502 and "empty" in r.json()["detail"]
    assert client.get("/drafts").json() == []


def test_mock_only_when_backend_is_mock(client, runtime):
    runtime.settings = runtime.settings.model_copy(update={"polytone_llm": "anthropic", "anthropic_api_key": ""})
    runtime._graphs.clear()
    r = client.post("/generate", json={"intent": "- tell sam hi"})
    assert r.status_code == 502 and "ANTHROPIC_API_KEY" in r.json()["detail"]


# =========================================================================== drafts never reach the graph
def _graph_snapshot(store: InMemoryGraphStore):
    return copy.deepcopy(
        (dict(store.words), {k: dict(v) for k, v in store.word_p.items()}, dict(store.bigrams),
         dict(store.structures), dict(store.punct), store.contexts)
    )


def test_generated_drafts_never_reach_graph_via_any_path(client, runtime, monkeypatch):
    _load_corpus(runtime)
    drafts = [
        client.post("/generate", json={"intent": i, "persona": p}).json()["text"]
        for i, p in (
            ("- tell dana the launch moved to friday because billing needs another week\n- ask for the deck", "Boss"),
            ("- tell jo i can't make it tonight\n- ask if tomorrow works instead", "Friend"),
        )
    ]
    assert all(len(tokenize(d)) >= 8 for d in drafts)
    variants = []
    for d in drafts:
        variants += [d, d.upper(), f"Hi all,\n\n{d}\n\nCheers", d.replace(".", "!"), d + " Thanks again"]
    before, n_samples = _graph_snapshot(runtime.store), runtime.db.count_samples()

    valid_prov = {
        "manual": {"source": "manual", "generated": False, "direction": "outbound"},
        "gmail": {"label": "SENT", "from_me": True, "direction": "outbound"},
        "wispr": {"raw_transcript": True},
        "watcher": {"kind": "mbox", "from_me": True, "file": "sent.mbox"},
    }
    for v in variants:  # HTTP manual path
        r = client.post("/ingest/manual", json={"text": v, "persona": "Boss"}).json()
        assert r["accepted"] == 0 and "draft" in r["reasons"][0], (v, r)
    for source, prov in valid_prov.items():  # direct run_ingest for every source
        for v in variants:
            r = run_ingest(v, source, "Friend", prov, runtime=runtime)
            assert r["accepted"] == 0 and "draft" in r["reasons"][0], (source, v, r)
    for name, modname in (("gmail", "gmail_outbound"), ("wispr", "wispr"), ("watcher", "file_watcher")):
        fake = types.ModuleType(f"backend.mcp_servers.{modname}")
        fake.is_enabled = lambda settings: True
        fake.collect = lambda settings, _p=valid_prov[name]: [{"text": v, "provenance": dict(_p)} for v in variants]
        monkeypatch.setitem(sys.modules, f"backend.mcp_servers.{modname}", fake)
        r = client.post(f"/ingest/sources/{name}/run").json()
        assert r["accepted"] == 0 and r["rejected"] == len(variants) and all("draft" in x for x in r["reasons"]), r

    assert _graph_snapshot(runtime.store) == before
    assert runtime.db.count_samples() == n_samples
    # sanity: the same paths still accept genuinely new human text
    r = client.post("/ingest/manual", json={"text": "brb grabbing coffee, back in 10", "persona": "Friend"}).json()
    assert r["accepted"] == 1 and _graph_snapshot(runtime.store) != before


@pytest.mark.parametrize("persona", ["Boss", "Friend"])
def test_example_constraints_json_shape(runtime, persona):
    _load_corpus(runtime)
    c = runtime.store.get_constraints(persona)
    json.dumps(c)  # serializable
    assert c["sample_count"] == 8 and c["structures"] and c["lexicon"] and c["top_bigrams"]
    assert all(0 < s["weight"] <= 1 for s in c["structures"])
