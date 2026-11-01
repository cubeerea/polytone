"""Integration tests against a SCRATCH Neo4j — never the user's style graph.

Opt-in: set POLYTONE_TEST_NEO4J_URI (and POLYTONE_TEST_NEO4J_PASSWORD), e.g. a throwaway
`docker run -d --rm --name polytone-test -p 7688:7687 -e NEO4J_AUTH=neo4j/test-pass neo4j:5-community`.
Without it these tests skip, even when the real graph on NEO4J_URI is up.

Every test uses a unique persona and words prefixed with a unique token, and removes
everything it created afterwards.
"""
import os
import time
import uuid

import pytest

from backend.config import Settings
from backend.graph.constraints import compute_constraints
from backend.graph.store import InMemoryGraphStore, Neo4jGraphStore, word_key
from backend.ingestion.extract import extract

pytestmark = pytest.mark.neo4j


@pytest.fixture
def neo():
    uri = os.environ.get("POLYTONE_TEST_NEO4J_URI")
    if not uri:
        pytest.skip("set POLYTONE_TEST_NEO4J_URI to a scratch Neo4j to run these")
    s = Settings(
        neo4j_uri=uri,
        neo4j_password=os.environ.get("POLYTONE_TEST_NEO4J_PASSWORD", "test-pass"),
    )
    store = Neo4jGraphStore.from_settings(s)
    if not store.healthy():
        store.close()
        pytest.skip("Neo4j not reachable")
    store.ensure_schema()
    tag = "zq" + uuid.uuid4().hex[:8]
    persona = f"__pytest_{tag}"
    created: dict[str, set] = {"sigs": set(), "syms": set()}
    yield store, tag, persona, created
    store._driver.execute_query(
        "MATCH (c:IntentContext) WHERE c.name STARTS WITH $p DETACH DELETE c", {"p": persona}
    )
    # legacy-format words (migration test) still carry plaintext text and no key
    store._driver.execute_query(
        "MATCH (w:Word) WHERE w.key IS NULL AND w.text STARTS WITH $t DETACH DELETE w", {"t": tag}
    )
    # robust to tokenization changes: also drop any Word only this test's personas ever used
    store._driver.execute_query(
        "MATCH (w:Word) WHERE size(w.personas) > 0 AND all(p IN w.personas WHERE p STARTS WITH $p) "
        "DETACH DELETE w",
        {"p": persona},
    )
    store._driver.execute_query(
        "MATCH (s:SentenceStructure) WHERE s.signature IN $sigs AND NOT (s)--() DELETE s",
        {"sigs": list(created["sigs"])},
    )
    store._driver.execute_query(
        "MATCH (p:PunctuationMark) WHERE p.symbol IN $syms AND NOT (p)--() DELETE p",
        {"syms": list(created["syms"])},
    )
    store.close()


def _texts(tag):
    w = lambda x: f"{tag}{x}"
    return [
        f"{w('hey')} — {w('i')} {w('don')}'t {w('think')} {w('so')}. {w('ok')} {w('then')}!",
        f"{w('hey')} {w('again')}... {w('the')} {w('deck')} {w('is')} {w('done')}.",
        f"{w('Honestly')} {w('fine')}. {w('hey')} {w('ok')}.",
    ]


def _write(store, created, text, persona):
    ex = extract(text)
    created["sigs"].update(ex.structures)
    created["syms"].update(ex.punctuation)
    store.write_extraction(ex, persona)
    return ex


def test_neo4j_matches_in_memory(neo):
    store, tag, persona, created = neo
    mem = InMemoryGraphStore()
    for t in _texts(tag):
        _write(store, created, t, persona)
        mem.write_extraction(extract(t), persona)
    # the live DB may hold other data (global blend), so compare the persona aggregate — the
    # Cypher reads — and persona-only constraints computed from it
    assert store._aggregate(persona, 20) == mem._aggregate(persona, 20)
    c_neo = compute_constraints(store._aggregate(persona, 20), 20)
    assert c_neo == compute_constraints(mem._aggregate(persona, 20), 20)
    assert store.get_constraints(persona)["sample_count"] == 3
    assert c_neo["sample_count"] == 3 and c_neo["casing"] == "lower"
    assert c_neo["top_openers"][0] == f"{tag}hey"
    assert c_neo["contractions_rate"] == 1.0 and c_neo["punctuation_rates"]["—"] > 0
    # persona weights on FOLLOWED_BY
    rows = store.execute_read(
        "MATCH (a:Word {key: $a})-[r:FOLLOWED_BY]->(b:Word {key: $b}) RETURN r.weight AS w,"
        " r.personas AS ps, r.persona_weights AS pw",
        {"a": word_key(f"{tag}hey"), "b": word_key(f"{tag}ok")},
    )
    assert rows == [{"w": 1, "ps": [persona], "pw": [1]}]
    # at rest: blind index + ciphertext only, never the word itself
    leaked = store.execute_read(
        "MATCH (w:Word) WHERE w.text CONTAINS $t RETURN count(w) AS n", {"t": tag}
    )
    assert leaked == [{"n": 0}]
    (ctx,) = store.execute_read("MATCH (c:IntentContext {name: $p}) RETURN properties(c) AS c", {"p": persona})
    assert all(k.startswith("idx1:") for k in ctx["c"]["opener_keys"])
    assert all(t.startswith("enc1:") for t in ctx["c"]["opener_texts"])
    assert not any(tag in str(v) for v in ctx["c"].values() if v != persona)


def test_neo4j_migrates_legacy_plaintext_graph(neo):
    """Plaintext nodes in the pre-encryption format are encrypted in place, in batches, once."""
    store, tag, persona, created = neo
    words = [f"{tag}w{i}" for i in range(5)]
    store._driver.execute_query(
        "UNWIND $ws AS w CREATE (:Word {text: w, weight: 1, personas: [$p], persona_counts: [1]})",
        {"ws": words, "p": persona},
    )
    store._driver.execute_query(
        "MATCH (a:Word {text: $a}), (b:Word {text: $b}) "
        "CREATE (a)-[:FOLLOWED_BY {weight: 1, personas: [$p], persona_weights: [1]}]->(b)",
        {"a": words[0], "b": words[1], "p": persona},
    )
    store._driver.execute_query(
        "CREATE (:IntentContext {name: $p, sample_count: 1, sentence_count: 1, lower_starts: 1,"
        " upper_starts: 0, len_sum: 5, len_hist: [0,0,0,0,0,1], contractions: 0, expansions: 0,"
        " opener_keys: [$o], opener_counts: [1], opener_phrase_keys: [], opener_phrase_counts: [],"
        " transition_keys: [], transition_counts: []})",
        {"p": persona, "o": words[0]},
    )
    store._driver.execute_query("MATCH (m:PolytoneMeta) DETACH DELETE m")  # scratch DB: force a run
    counts = store.migrate_encryption(batch=2)
    assert counts["words"] >= 5 and counts["contexts"] >= 1
    assert store.migrate_encryption() == {}  # idempotent (marker set)
    assert store.execute_read(
        "MATCH (w:Word) WHERE w.text STARTS WITH $t OR w.key IS NULL RETURN count(w) AS n", {"t": tag}
    ) == [{"n": 0}]
    store._plain.clear()  # read back through decryption, not the write-side memo
    agg = store._aggregate(persona, 20)
    assert agg.words == {w: 1 for w in words}
    assert agg.bigrams == {(words[0], words[1]): 1}
    assert agg.openers == {words[0]: 1}
    # writes after the migration merge into the migrated nodes (same blind index)
    _write(store, created, f"{words[0]} {words[1]} {tag}more text.", persona)
    store._plain.clear()
    agg = store._aggregate(persona, 20)
    assert agg.words[words[0]] == 2 and agg.openers[words[0]] == 2


def test_neo4j_graph_and_stats(neo):
    store, tag, persona, created = neo
    before = store.stats()
    for t in _texts(tag):
        _write(store, created, t, persona)
    after = store.stats()
    assert after["samples"] == before["samples"] + 3
    assert after["words"] >= before["words"] + 10
    g = store.get_graph(30, persona=persona)
    ids = {n["id"] for n in g["nodes"]}
    assert f"c:{persona}" in ids and f"w:{tag}hey" in ids
    types = {e["type"] for e in g["edges"]}
    assert {"FOLLOWED_BY", "USED_IN"} <= types
    assert all(e["source"] in ids and e["target"] in ids for e in g["edges"])


def test_neo4j_retrieval_fast_and_cached(neo):
    store, tag, persona, created = neo
    for i in range(30):
        _write(store, created, f"{tag}a{i} {tag}b {tag}c{i % 5}. {tag}d {tag}e{i}!", persona)
    store.invalidate()
    t = time.perf_counter()
    c = store.get_constraints(persona)
    assert time.perf_counter() - t < 0.3
    assert c["sample_count"] == 30
    t = time.perf_counter()
    store.get_constraints(persona)
    assert time.perf_counter() - t < 0.01
    _write(store, created, f"{tag}new {tag}sample.", persona)  # invalidates the cache
    assert store.get_constraints(persona)["sample_count"] == 31


def test_neo4j_end_to_end_runtime(neo, settings, tmp_path):
    from backend.langgraph.generate_graph import run_generation
    from backend.langgraph.ingest_graph import run_ingest
    from backend.llm.client import MockLLM
    from backend.runtime import build_runtime

    store, tag, persona, created = neo
    rt = build_runtime(settings, db_path=tmp_path / "n.sqlite3", store=store, llm=MockLLM(), scorer=False)
    for t in _texts(tag):
        created["sigs"].update(extract(t).structures)
        created["syms"].update(extract(t).punctuation)
        assert run_ingest(t, "manual", persona, {}, runtime=rt)["accepted"] == 1
    out = run_generation("- tell sam meeting moved to 3", persona, None, runtime=rt)
    assert out["constraints"]["sample_count"] == 3
    r = run_ingest(out["text"], "manual", persona, {}, runtime=rt)
    assert r["rejected"] == 1
    assert store.get_constraints(persona)["sample_count"] == 3


def _tagged(tag: str, text: str) -> str:
    """Prefix every word with the test tag (keeping its case) so cleanup can find it."""
    import re

    return re.sub(r"(?<![\w'’@.])([A-Za-z])", lambda m: (tag.upper() if m.group(1).isupper() else tag) + m.group(1), text)


def test_neo4j_fixture_corpus_personas(neo):
    """The realistic corpus through the live graph: parity with memory, personas differ, fast."""
    import json
    from pathlib import Path

    store, tag, persona, created = neo
    corpus = json.loads((Path(__file__).parent / "fixtures" / "corpus.json").read_text())
    boss, friend = f"{persona}_boss", f"{persona}_friend"
    mem = InMemoryGraphStore()
    try:
        for p, key in ((boss, "Boss"), (friend, "Friend")):
            for t in corpus[key]:
                tt = _tagged(tag, t)
                _write(store, created, tt, p)
                mem.write_extraction(extract(tt), p)
        for p in (boss, friend):
            assert store._aggregate(p, 20) == mem._aggregate(p, 20)
        store.invalidate()
        t0 = time.perf_counter()
        b = store.get_constraints(boss)
        assert time.perf_counter() - t0 < 0.3
        f = store.get_constraints(friend)
        assert b["sample_count"] == 8 and f["sample_count"] == 8
        assert b["casing"] == "sentence" and f["casing"] == "lower"
        assert b["avg_sentence_len"] > f["avg_sentence_len"]
        assert "emoji" not in b["punctuation_rates"] and f["punctuation_rates"]["emoji"] > 0.1
        assert b["top_openers"][0] == f"{tag}quick {tag}update {tag}on"
        # (tag-prefixed words defeat the stopword lists, so check distinctive words directly)
        assert f"{tag}migration" in b["lexicon"] and f"{tag}migration" not in f["lexicon"]
        assert f"{tag}lol" in f["lexicon"] and f"{tag}lol" not in b["lexicon"]
        assert [f"{tag}quick", f"{tag}update", 4.0] in b["top_bigrams"]
    finally:
        for p in (boss, friend):
            store._driver.execute_query("MATCH (c:IntentContext {name: $p}) DETACH DELETE c", {"p": p})


def test_neo4j_merge_persona_matches_in_memory(neo):
    """Deleting a persona folds its learning into another one; Cypher and in-memory agree."""
    store, tag, persona, created = neo
    dst = persona + "_dst"
    mem = InMemoryGraphStore()
    for i, t in enumerate(_texts(tag)):
        p = persona if i % 2 == 0 else dst
        _write(store, created, t, p)
        mem.write_extraction(extract(t), p)
    assert store.merge_persona(persona, dst) == 2
    assert mem.merge_persona(persona, dst) == 2
    assert store._aggregate(dst, 20) == mem._aggregate(dst, 20)
    assert store._aggregate(dst, 20).sample_count == 3
    assert store.execute_read("MATCH (c:IntentContext {name: $p}) RETURN c", {"p": persona}) == []
    rows = store.execute_read(
        "MATCH (w:Word) WHERE $p IN w.personas RETURN count(w) AS n", {"p": persona}
    )
    assert rows == [{"n": 0}]
    rows = store.execute_read(
        "MATCH (a:Word {key: $a})-[r:FOLLOWED_BY]->(b:Word {key: $b}) RETURN r.weight AS w,"
        " r.personas AS ps, r.persona_weights AS pw",
        {"a": word_key(f"{tag}hey"), "b": word_key(f"{tag}ok")},
    )
    assert rows and rows[0]["ps"] == [dst] and rows[0]["pw"] == [rows[0]["w"]]
    ctx = store.execute_read("MATCH (c:IntentContext {name: $p}) RETURN properties(c) AS c", {"p": dst})[0]["c"]
    assert len(ctx["opener_keys"]) == len(ctx["opener_texts"]) == len(ctx["opener_counts"])
    assert store.merge_persona(persona, dst) == 0  # idempotent
