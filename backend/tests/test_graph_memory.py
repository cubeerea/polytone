import time

from backend.graph.constraints import empty_constraints
from backend.graph.store import GraphStore, InMemoryGraphStore
from backend.ingestion.extract import extract

from .conftest import HUMAN_SAMPLES

CONSTRAINT_KEYS = set(empty_constraints())


def test_empty_store_constraints():
    s = InMemoryGraphStore()
    assert isinstance(s, GraphStore)
    c = s.get_constraints("Default")
    assert c == empty_constraints() and set(c) == CONSTRAINT_KEYS
    assert s.get_graph(10) == {"nodes": [], "edges": []}
    assert s.stats() == {"words": 0, "structures": 0, "samples": 0, "avg_sentence_len": None}


def test_write_and_constraints():
    s = InMemoryGraphStore()
    for t in HUMAN_SAMPLES:
        r = s.write_extraction(extract(t), "Friend")
        assert r["sentences"] > 0 and r["words_merged"] > 0
    c = s.get_constraints("Friend")
    assert c["sample_count"] == 3
    assert c["casing"] == "lower"
    assert c["contractions_rate"] == 1.0
    assert 4 < c["avg_sentence_len"] < 8 and c["sentence_len_p90"] >= c["avg_sentence_len"]
    assert c["punctuation_rates"]["—"] > 0 and "…" in c["punctuation_rates"]
    assert "honestly" in c["top_openers"] and "so" in c["top_transitions"]
    assert "deck" in c["lexicon"] and "the" not in c["lexicon"]
    assert all(len(b) == 3 for b in c["top_bigrams"])
    assert abs(sum(x["weight"] for x in c["structures"]) - 1.0) < 0.01  # all structures fit in top-k


def test_persona_scoping_and_fallback():
    s = InMemoryGraphStore()
    s.write_extraction(extract("Dear team. I am pleased to report strong growth."), "Investor")
    s.write_extraction(extract("lol ok. see u there!"), "Friend")
    inv, fr = s.get_constraints("Investor"), s.get_constraints("Friend")
    assert inv["sample_count"] == 1 and fr["sample_count"] == 1
    assert "growth" in inv["lexicon"] and "growth" not in fr["lexicon"]
    # 1 sample each → alpha = 0.25: cadence is shrunk towards the overall style, but still differs
    assert fr["casing"] == "lower" and inv["casing"] != "lower"
    # a persona with no data falls back to all personas
    boss = s.get_constraints("Boss")
    assert boss["sample_count"] == 2 and "growth" in boss["lexicon"]
    g = s.get_graph(30, persona="Friend")
    names = {n["name"] for n in g["nodes"] if n["label"] == "Word"}
    assert "lol" in names and "growth" not in names


def test_cache_invalidation_and_speed():
    s = InMemoryGraphStore()
    s.write_extraction(extract(HUMAN_SAMPLES[0]), "Default")
    c1 = s.get_constraints("Default")
    c1["lexicon"].append("mutated")  # callers get copies
    assert "mutated" not in s.get_constraints("Default")["lexicon"]
    s.write_extraction(extract(HUMAN_SAMPLES[1]), "Default")
    assert s.get_constraints("Default")["sample_count"] == 2
    for i in range(200):
        s.write_extraction(extract(f"word{i} and more{i} things here."), "Default")
    t = time.perf_counter()
    s.get_constraints("Default")
    assert time.perf_counter() - t < 0.3
    t = time.perf_counter()
    s.get_constraints("Default")
    assert time.perf_counter() - t < 0.01  # cached


def test_graph_shape():
    s = InMemoryGraphStore()
    for t in HUMAN_SAMPLES:
        s.write_extraction(extract(t), "Default")
    g = s.get_graph(25)
    assert 0 < len(g["nodes"]) <= 25
    ids = {n["id"] for n in g["nodes"]}
    assert len(ids) == len(g["nodes"])
    labels = {n["label"] for n in g["nodes"]}
    assert labels <= {"Word", "SentenceStructure", "PunctuationMark", "IntentContext"}
    assert {"Word", "IntentContext", "PunctuationMark"} <= labels
    types = {e["type"] for e in g["edges"]}
    assert "FOLLOWED_BY" in types and "USED_IN" in types
    for e in g["edges"]:
        assert e["source"] in ids and e["target"] in ids and e["weight"] > 0
    st = s.stats()
    assert st["samples"] == 3 and st["words"] > 10 and st["avg_sentence_len"] > 0


def test_accepts_dict_extraction():
    s = InMemoryGraphStore()
    s.write_extraction(extract("hello there friend.").to_dict(), "Default")
    assert s.get_constraints(None)["sample_count"] == 1
