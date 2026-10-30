"""Graph MCP server: read-only guard, tool impls, and a real stdio round trip."""
from __future__ import annotations

import asyncio

import pytest

from backend.langgraph.ingest_graph import run_ingest
from backend.mcp_servers import client, graph_server
from backend.mcp_servers.graph_server import ReadOnlyViolation, check_read_only
from backend.tests.conftest import HUMAN_SAMPLES

WRITES = [
    "CREATE (n:Word {text:'x'})",
    "MATCH (n) DETACH DELETE n",
    "MATCH (n) DELETE n",
    "MERGE (w:Word {text:'a'}) RETURN w",
    "MATCH (w:Word) SET w.weight = 0",
    "match (w:Word) set w.weight = 0",
    "MATCH (w:Word) REMOVE w.weight",
    "DROP CONSTRAINT word_text",
    "LOAD CSV FROM 'file:///x.csv' AS row RETURN row",
    "CALL dbms.security.createUser('a','b')",
    "CALL apoc.create.node(['Word'], {}) YIELD node RETURN node",
    "CALL apoc.cypher.runWrite('CREATE (n)', {})",
    "CALL db.createLabel('Foo')",
    "MATCH (n) CALL { WITH n DELETE n } IN TRANSACTIONS",
    "MATCH (n) FOREACH (x IN [1] | SET n.a = x)",
    "MATCH (w:Word)\n//comment\nWITH w CREATE (w)-[:X]->(w)",
    "",
]

READS = [
    "MATCH (w:Word) RETURN w.text AS t ORDER BY w.weight DESC LIMIT 5",
    "MATCH (w:Word {text: 'set'}) RETURN w",  # keyword inside a string literal
    "MATCH (w:Word) WHERE w.text STARTS WITH 'cre' RETURN count(w) AS c",
    "MATCH (s:SentenceStructure)-[r:USED_IN]->(c:IntentContext {name:$p}) RETURN s.signature, r.weight",
    "CALL db.labels() YIELD label RETURN label",
    "// CREATE in a comment\nMATCH (n) RETURN count(n) AS c",
    "MATCH (`create`:Word) RETURN `create`.text",
]


@pytest.mark.parametrize("q", WRITES)
def test_write_queries_rejected(q):
    with pytest.raises(ReadOnlyViolation):
        check_read_only(q)


@pytest.mark.parametrize("q", READS)
def test_read_queries_allowed(q):
    check_read_only(q)


class _FakeNeo4jStore:
    kind = "neo4j"

    def __init__(self):
        self.calls = []

    def execute_read(self, query, params):
        self.calls.append((query, params))
        return [{"t": "hello", "n": 3}]


def test_execute_read_impl_never_reaches_store_on_write(runtime):
    fake = _FakeNeo4jStore()
    runtime.store = fake
    out = graph_server.execute_read_impl("MATCH (n) DETACH DELETE n", {}, rt=runtime)
    assert out["ok"] is False and "DETACH" in out["error"] and fake.calls == []
    out = graph_server.execute_read_impl("MATCH (w:Word) RETURN w.text AS t", {"x": 1}, rt=runtime)
    assert out == {"ok": True, "rows": [{"t": "hello", "n": 3}], "truncated": False}
    assert fake.calls == [("MATCH (w:Word) RETURN w.text AS t", {"x": 1})]


def test_execute_read_impl_memory_store(runtime):
    out = graph_server.execute_read_impl("MATCH (n) RETURN n", {}, rt=runtime)
    assert out["ok"] is False and "Neo4j" in out["error"]


def test_get_constraints_and_stats_impl(runtime):
    for s in HUMAN_SAMPLES:
        run_ingest(s, "manual", "Friend", {"generated": False}, runtime=runtime)
    c = graph_server.get_constraints_impl("Friend", 10, rt=runtime)
    assert c == dict(runtime.store.get_constraints("Friend", 10))
    assert c["sample_count"] == len(HUMAN_SAMPLES)
    stats = graph_server.graph_stats_impl(rt=runtime)
    assert stats["graph_store"] == "memory" and stats["samples_sqlite"] == len(HUMAN_SAMPLES)


def test_mcp_stdio_end_to_end(tmp_path):
    """Spawn the server over stdio and call its tools through a real MCP client session."""
    env = {"POLYTONE_DATA_DIR": str(tmp_path), "POLYTONE_GRAPH_STORE": "memory", "POLYTONE_LLM": "mock"}
    tools = asyncio.run(client.list_tools_async(env=env))
    assert {"execute_read", "get_constraints", "graph_stats"} <= set(tools)
    c = client.get_constraints_via_mcp("Default", 5, env=env)
    assert set(c) >= {"avg_sentence_len", "top_bigrams", "structures", "lexicon", "sample_count"}
    assert c["sample_count"] == 0
    w = client.execute_read_via_mcp("MATCH (n) DETACH DELETE n", env=env)
    assert w["ok"] is False and "read-only" in w["error"]
    stats = client.call_tool("graph_stats", env=env)
    assert stats["graph_store"] == "memory"


@pytest.mark.neo4j
def test_neo4j_read_transaction_refuses_writes(tmp_path):
    """Defence in depth: even if the static guard were bypassed, the READ tx refuses writes."""
    from backend.config import Settings
    from backend.graph.store import Neo4jGraphStore

    try:
        store = Neo4jGraphStore.from_settings(Settings(polytone_data_dir=tmp_path))
        if not store.healthy():
            pytest.skip("neo4j not running")
    except Exception:  # noqa: BLE001
        pytest.skip("neo4j not running")
    try:
        with pytest.raises(Exception):  # noqa: B017 - driver raises a ClientError subclass
            store.execute_read("CREATE (n:PolytoneMcpProbe) RETURN n", {})
        assert store.execute_read("MATCH (n:PolytoneMcpProbe) RETURN count(n) AS c", {})[0]["c"] == 0
    finally:
        store.close()
