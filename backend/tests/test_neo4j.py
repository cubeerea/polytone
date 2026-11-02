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
from backend.graph import queries as Q
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
        polytone_decay_half_life_days=0,  # decay tests build their own stores (fake clock)
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


def test_neo4j_large_redundant_text_stays_bounded(neo):
    """A large, repetitive sample (tens of thousands of words, common words repeated thousands
    of times) writes bounded counts — not a node or edge per occurrence — and matches the
    in-memory store exactly."""
    from backend.graph import queries as Q
    from backend.graph import trim
    from backend.tests.test_scalability import big_text

    store, tag, persona, created = neo
    text = big_text(n_sentences=3000, n_rare=5000, prefix=tag).replace("the ", f"{tag}the ")
    hub = f"{tag}the"
    ex = extract(text)
    assert len(ex.words) > trim.MAX_WORDS and ex.words[hub] > 9000
    before = store.execute_read(Q.GRAPH_SIZE)[0]
    t0 = time.perf_counter()
    for _ in range(3):  # the same text again: counts grow, the graph does not
        _write(store, created, text, persona)
    elapsed = time.perf_counter() - t0
    after = store.execute_read(Q.GRAPH_SIZE)[0]
    assert elapsed < 60, f"three large writes took {elapsed:.1f}s"
    assert after["words"] - before["words"] <= trim.MAX_WORDS
    assert after["followed_by"] - before["followed_by"] <= trim.MAX_BIGRAMS
    assert after["part_of"] - before["part_of"] <= trim.MAX_WORD_STRUCTURES
    (deg,) = store.execute_read(Q.WORD_DEGREES, {"keys": [word_key(hub)]})
    assert deg["weight"] == 3 * ex.words[hub]
    assert deg["out"] <= trim.SAMPLE_FANOUT and deg["inn"] <= trim.SAMPLE_FANOUT
    mem = InMemoryGraphStore()
    for _ in range(3):
        mem.write_extraction(ex, persona)
    assert store._aggregate(persona, 20) == mem._aggregate(persona, 20)


def test_neo4j_common_word_fanout_is_capped(neo):
    """Over many samples a common word keeps only its GRAPH_FANOUT strongest FOLLOWED_BY edges
    per direction — the same ones the in-memory store keeps."""
    from backend.graph import queries as Q
    from backend.graph import trim
    from backend.tests.test_scalability import rare_words

    store, tag, persona, created = neo
    hub, cat = f"{tag}the", f"{tag}cat"
    rare = rare_words(trim.GRAPH_FANOUT * 3, prefix=tag)
    mem = InMemoryGraphStore()
    for i in range(0, len(rare), 50):
        body = " ".join(f"{hub} {w} and {w} {hub}." for w in rare[i : i + 50])
        text = body + f" {hub} {cat} and {hub} {cat}."
        _write(store, created, text, persona)
        mem.write_extraction(extract(text), persona)
    (deg,) = store.execute_read(Q.WORD_DEGREES, {"keys": [word_key(hub)]})
    assert deg["out"] == trim.GRAPH_FANOUT and deg["inn"] == trim.GRAPH_FANOUT
    kept = store.execute_read(
        "MATCH (a:Word {key: $a})-[r:FOLLOWED_BY]->(b:Word) RETURN b.key AS b, r.weight AS n",
        {"a": word_key(hub)},
    )
    assert {r["b"]: r["n"] for r in kept} == {word_key(b): n for (a, b), n in mem.bigrams.items() if a == hub}
    assert {r["b"]: r["n"] for r in kept}[word_key(cat)] > 1  # the frequent edge survived
    assert store._aggregate(persona, 20) == mem._aggregate(persona, 20)


# ----------------------------------------------------------------------------- recency decay
# graph/decay.py. The stores under test get a fake clock and their own half-life. The decay epoch
# lives in the shared (:PolytoneMeta {name:'schema'}) node, so ``decay_neo`` pins it to the fake
# clock for the test and restores it afterwards (rebase_epoch is kept: epochs only ever grow).
# Rebase and prune act on the WHOLE scratch database — one more reason it must be a scratch one.
_DECAY_META = ("decay_t0", "decay_half_life_s", "rebase_target_t0", "rebase_factor", "rebase_half_life_s")
_START = 1_750_000_000.0


class _Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, days: float) -> None:
        self.t += days * 86400.0


@pytest.fixture
def decay_neo(neo):
    store, tag, persona, created = neo
    rows = store.execute_read("MATCH (m:PolytoneMeta {name: 'schema'}) RETURN properties(m) AS m")
    saved = {k: rows[0]["m"].get(k) for k in _DECAY_META} if rows else {}
    clock = _Clock(_START)
    opened: list[Neo4jGraphStore] = []

    def make(half_life_days: float, rebase_at: float = 1e6, *, set_epoch: bool = True) -> Neo4jGraphStore:
        if set_epoch:  # the epoch the in-memory twin also uses: t0 = the fake clock's start
            store._driver.execute_query(
                "MERGE (m:PolytoneMeta {name: 'schema'}) SET m.decay_t0 = $t0, m.decay_half_life_s = $h "
                "REMOVE m.rebase_target_t0, m.rebase_factor, m.rebase_half_life_s",
                {"t0": _START, "h": half_life_days * 86400.0},
            )
        s = Settings(
            neo4j_uri=os.environ["POLYTONE_TEST_NEO4J_URI"],
            neo4j_password=os.environ.get("POLYTONE_TEST_NEO4J_PASSWORD", "test-pass"),
            polytone_decay_half_life_days=half_life_days,
        )
        st = Neo4jGraphStore.from_settings(s, clock=clock)
        st.decay.rebase_at = rebase_at
        st.decay_batch = 7  # many small inner transactions
        opened.append(st)
        return st

    yield make, clock, store, tag, persona, created
    for st in opened:
        st.close()
    if rows:
        store._driver.execute_query(
            "MATCH (m:PolytoneMeta {name: 'schema'}) REMOVE " + ", ".join(f"m.{k}" for k in _DECAY_META)
            + " SET m += $set",
            {"set": {k: v for k, v in saved.items() if v is not None}},
        )
    else:
        store._driver.execute_query("MATCH (m:PolytoneMeta {name: 'schema'}) DETACH DELETE m")


def _style_texts(tag):
    old = [_tagged(tag, t) for t in (
        "lol ok see u at the park. lol that was fun 😂 lol same time tmrw?",
        "lol no way. lol u serious 😂 lol fine.",
        "lol ok. lol brb 😂 lol later dude.",
    )]
    new = [_tagged(tag, t) for t in (
        "Regarding the report, I reviewed the figures; they look solid. Regarding the budget, confirm by Friday.",
        "Regarding the plan, the team needs a week; the rollout moves. Regarding staffing, we hire two.",
        "Regarding the contract, legal approved the terms; signatures pend. Regarding invoices, pay Tuesday.",
    )]
    return old, new


def _twin_writes(neo_store, mem, created, texts, persona):
    for t in texts:
        _write(neo_store, created, t, persona)
        mem.write_extraction(extract(t), persona)


def _edge_set(g):
    return sorted((e["source"], e["target"], e["type"], e["weight"]) for e in g["edges"])


def test_neo4j_decay_parity_with_in_memory(decay_neo):
    """Same fake clock, same t0: Cypher and the in-memory store hold the same floats and return
    identical aggregates (ranked on stored weights, divided by s(now) in Python) — exactly."""
    make, clock, _, tag, persona, created = decay_neo
    store = make(10)
    mem = InMemoryGraphStore(half_life_days=10, clock=clock, t0=_START)
    old, new = _style_texts(tag)
    _twin_writes(store, mem, created, old, persona)
    clock.advance(35)
    _twin_writes(store, mem, created, new, persona)
    assert store.decay.t0 == mem.decay.t0 == _START
    a_neo, a_mem = store._aggregate(persona, 20), mem._aggregate(persona, 20)
    assert a_neo == a_mem
    assert a_neo.sample_count == 6 and isinstance(a_neo.sample_count, int)
    assert a_neo.sample_weight == pytest.approx(3 / 2 ** 3.5 + 3)
    c = compute_constraints(a_neo, 20)
    assert c == compute_constraints(a_mem, 20)
    assert c["casing"] == "sentence" and c["top_openers"][0].startswith(f"{tag}regarding")
    # at rest: float weights, scaled at write time
    (deg,) = store.execute_read(Q.WORD_DEGREES, {"keys": [word_key(f"{tag}regarding")]})
    assert deg["weight"] == mem.words[f"{tag}regarding"] == pytest.approx(6 * 2 ** 3.5)
    clock.advance(40)  # idle: still identical, effective counts lower
    a_neo = store._aggregate(persona, 20)
    assert a_neo == mem._aggregate(persona, 20)
    assert a_neo.words[f"{tag}regarding"] == pytest.approx(6 / 2 ** 4)
    g_neo, g_mem = store.get_graph(40, persona), mem.get_graph(40, persona)
    assert g_neo["nodes"] == g_mem["nodes"] and _edge_set(g_neo) == _edge_set(g_mem)


def test_neo4j_decay_rebase_matches_in_memory(decay_neo):
    make, clock, _, tag, persona, created = decay_neo
    store = make(10, rebase_at=4.0)
    mem = InMemoryGraphStore(half_life_days=10, clock=clock, t0=_START, rebase_at=4.0)
    old, new = _style_texts(tag)
    _twin_writes(store, mem, created, old, persona)
    clock.advance(30)  # s = 8 > 4
    _twin_writes(store, mem, created, new, persona)
    before = store._aggregate(persona, 20)
    assert store.decay_maintenance()["rebased"] == 1 and mem.decay_maintenance()["rebased"] == 1
    meta = store._meta()
    assert meta["decay_t0"] == clock.t == store.decay.t0 and meta.get("rebase_target_t0") is None
    after = store._aggregate(persona, 20)
    assert after == mem._aggregate(persona, 20)  # same factor, same products: exact
    assert after.sample_count == before.sample_count
    for w, n in before.words.items():
        assert after.words[w] == pytest.approx(n, rel=1e-12)
    def order(a):
        return sorted(a.words, key=lambda w: (-a.words[w], w))

    assert order(after) == order(before)
    assert compute_constraints(after, 20)["top_openers"] == compute_constraints(before, 20)["top_openers"]
    (deg,) = store.execute_read(Q.WORD_DEGREES, {"keys": [word_key(f"{tag}regarding")]})
    assert deg["weight"] == mem.words[f"{tag}regarding"] == pytest.approx(6.0)  # stored units shrank by 8
    rows = store.execute_read(  # every item stamped with the epoch, exactly once
        "MATCH (w:Word) WHERE w.key IN $keys RETURN collect(DISTINCT w.decay_epoch) AS e",
        {"keys": [word_key(w) for w in before.words]},
    )
    assert rows[0]["e"] == [meta["rebase_epoch"]]
    clock.advance(5)  # writes after the rebase are in the new units on both sides
    _twin_writes(store, mem, created, old[:1], persona)
    assert store._aggregate(persona, 20) == mem._aggregate(persona, 20)


def test_neo4j_decay_rebase_resumes_after_crash(decay_neo):
    """A crash after some REBASE queries committed: the next start-up finishes the same rebase
    (recorded factor / target) and never multiplies an already-stamped item again."""
    make, clock, _, tag, persona, created = decay_neo
    store = make(10, rebase_at=4.0)
    mem = InMemoryGraphStore(half_life_days=10, clock=clock, t0=_START, rebase_at=4.0)
    old, new = _style_texts(tag)
    _twin_writes(store, mem, created, old, persona)
    clock.advance(30)
    _twin_writes(store, mem, created, new, persona)
    real, calls = store._run_batched, []

    def crash_after_two(query, **params):
        if len(calls) == 2:  # Word and FOLLOWED_BY are rebased and stamped, the rest is not
            raise ConnectionError("simulated crash")
        calls.append(query)
        return real(query, **params)

    store._run_batched = crash_after_two
    with pytest.raises(ConnectionError):
        store.decay_maintenance()
    meta = store._meta()
    assert meta["decay_t0"] == _START and meta["rebase_target_t0"] == clock.t  # pending
    assert meta["rebase_factor"] == 0.125
    (deg,) = store.execute_read(Q.WORD_DEGREES, {"keys": [word_key(f"{tag}regarding")]})
    assert deg["weight"] == pytest.approx(6.0)  # half done: words already rebased
    # "restart": a new store finishes the pending rebase before serving anything
    restarted = make(10, rebase_at=4.0, set_epoch=False)
    assert restarted.init_decay() == {"resumed": 1}
    meta = restarted._meta()
    assert meta.get("rebase_target_t0") is None and meta["decay_t0"] == restarted.decay.t0 == clock.t
    mem.decay_maintenance()  # the clean rebase, at the same instant
    assert restarted._aggregate(persona, 20) == mem._aggregate(persona, 20)  # nothing applied twice
    (deg,) = store.execute_read(Q.WORD_DEGREES, {"keys": [word_key(f"{tag}regarding")]})
    assert deg["weight"] == pytest.approx(6.0)
    assert restarted.init_decay() == {}  # idempotent


def test_neo4j_decay_prune_matches_in_memory(decay_neo):
    make, clock, _, tag, persona, created = decay_neo
    store = make(1)
    mem = InMemoryGraphStore(half_life_days=1, clock=clock, t0=_START)
    gone = [f"{tag}zebra", f"{tag}quokka", f"{tag}marmot"]
    _twin_writes(store, mem, created, [f"{gone[0]} {gone[1]} {gone[2]}. {tag}the {gone[0]} {tag}sleeps."], persona)
    clock.advance(10)  # 2^-10 < PRUNE_BELOW
    _twin_writes(store, mem, created, [f"{tag}the {tag}cat {tag}sat. {tag}the {tag}dog {tag}sat."], persona)
    keys = [word_key(w) for w in gone]
    assert len(store.execute_read(Q.WORD_DEGREES, {"keys": keys})) == 3
    out, out_mem = store.decay_maintenance(), mem.decay_maintenance()
    assert out["rebased"] == 0
    for k in ("followed_by", "part_of", "words"):  # the scratch DB may hold more faded data
        assert out[k] >= out_mem[k] > 0, k
    assert store.execute_read(Q.WORD_DEGREES, {"keys": keys}) == []
    rows = store.execute_read(
        "MATCH (a:Word {key: $a})-[r:FOLLOWED_BY]->(b:Word {key: $b}) RETURN r.weight AS w",
        {"a": word_key(f"{tag}the"), "b": word_key(f"{tag}cat")},
    )
    assert rows and rows[0]["w"] == mem.bigrams[(f"{tag}the", f"{tag}cat")]
    assert store._aggregate(persona, 20) == mem._aggregate(persona, 20)
    assert store.decay_maintenance()["followed_by"] == 0


def test_neo4j_decay_merge_persona_matches_in_memory(decay_neo):
    make, clock, _, tag, persona, created = decay_neo
    store = make(10)
    mem = InMemoryGraphStore(half_life_days=10, clock=clock, t0=_START)
    dst = persona + "_dst"
    old, new = _style_texts(tag)
    _twin_writes(store, mem, created, old, persona)
    clock.advance(15)
    _twin_writes(store, mem, created, new, dst)
    assert store.merge_persona(persona, dst) == mem.merge_persona(persona, dst) == 3
    assert store._aggregate(dst, 20) == mem._aggregate(dst, 20)
    ctx = store.execute_read("MATCH (c:IntentContext {name: $p}) RETURN properties(c) AS c", {"p": dst})[0]["c"]
    assert ctx["sample_count"] == 6 and ctx["sample_weight"] == pytest.approx(3 + 3 * 2 ** 1.5)


def test_neo4j_decay_epoch_init_and_half_life_change(decay_neo):
    """t0 is recorded on first start; changing the half-life (or turning decay off) rebases to
    now with the half-life the data was written with, so effective counts carry over."""
    make, clock, base, tag, persona, created = decay_neo
    base._driver.execute_query(
        "MERGE (m:PolytoneMeta {name: 'schema'}) "
        "REMOVE m.decay_t0, m.decay_half_life_s, m.rebase_target_t0, m.rebase_factor, m.rebase_half_life_s"
    )
    store = make(10, set_epoch=False)
    assert store.init_decay() == {} and store._meta()["decay_t0"] == _START  # recorded now
    old, _ = _style_texts(tag)
    for t in old:
        _write(store, created, t, persona)
    clock.advance(10)
    before = store._aggregate(persona, 20)
    assert before.words[f"{tag}lol"] == pytest.approx(9 / 2)  # 9 uses, one half-life ago
    changed = make(20, set_epoch=False)
    assert changed.init_decay() == {"rebased": 1}
    assert changed._meta()["decay_half_life_s"] == 20 * 86400 and changed.decay.t0 == clock.t
    for w, n in before.words.items():
        assert changed._aggregate(persona, 20).words[w] == pytest.approx(n)
    off = make(0, set_epoch=False)
    assert off.init_decay() == {"rebased": 1}  # decay off: rebased to now, then s ≡ 1
    assert off._aggregate(persona, 20).words[f"{tag}lol"] == pytest.approx(9 / 2)
    assert off.decay_maintenance() == {}
    back_on = make(10, set_epoch=False)
    assert back_on.init_decay() == {"rebased": 1}  # factor 1: only the epoch moves to now
    assert back_on._aggregate(persona, 20).words[f"{tag}lol"] == pytest.approx(9 / 2)
