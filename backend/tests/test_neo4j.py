"""Integration tests against the live Neo4j container (docker compose up neo4j).

Every test uses a unique persona and words prefixed with a unique token, and removes
everything it created afterwards.
"""
import time
import uuid

import pytest

from backend.config import Settings
from backend.graph.store import InMemoryGraphStore, Neo4jGraphStore
from backend.ingestion.extract import extract

pytestmark = pytest.mark.neo4j


@pytest.fixture
def neo():
    s = Settings()
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
        "MATCH (c:IntentContext {name: $p}) DETACH DELETE c", {"p": persona}
    )
    store._driver.execute_query(
        "MATCH (w:Word) WHERE w.text STARTS WITH $t DETACH DELETE w", {"t": tag}
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
    c_neo = store.get_constraints(persona)
    c_mem = mem.get_constraints(persona)
    assert c_neo == c_mem
    assert c_neo["sample_count"] == 3 and c_neo["casing"] == "lower"
    assert c_neo["top_openers"][0] == f"{tag}hey"
    assert c_neo["contractions_rate"] == 1.0 and c_neo["punctuation_rates"]["—"] > 0
    # persona weights on FOLLOWED_BY
    rows = store.execute_read(
        "MATCH (a:Word {text: $a})-[r:FOLLOWED_BY]->(b:Word {text: $b}) RETURN r.weight AS w,"
        " r.personas AS ps, r.persona_weights AS pw",
        {"a": f"{tag}hey", "b": f"{tag}ok"},
    )
    assert rows == [{"w": 1, "ps": [persona], "pw": [1]}]


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
