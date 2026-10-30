"""Graph MCP server (stdio): read-only access to the style graph.

Tools
-----
- ``execute_read(query, params)`` — run a READ-ONLY Cypher query. Two layers of protection:
  (1) a static check rejects any write clause (CREATE/MERGE/DELETE/DETACH/SET/REMOVE/DROP/
  FOREACH/LOAD CSV/... IN TRANSACTIONS, CALL dbms.*/apoc.*/gds.* and db.* write procedures),
  (2) the query runs in a Neo4j READ transaction, so the server refuses writes anyway.
- ``get_constraints(persona, k)`` — the top-k structural constraints used by generation
  (identical to ``GraphStore.get_constraints``, which ``/generate`` calls in-process).
- ``graph_stats()`` — node / sample counts.

Run: ``uv run python -m backend.mcp_servers.graph_server``.
``POLYTONE_GRAPH_STORE=memory`` forces the in-memory store (tests / no Neo4j).
"""
from __future__ import annotations

import os
import re
import sys
from typing import Any

from backend.mcp_servers._common import make_server

MAX_ROWS = 1000

_WRITE_KEYWORDS = re.compile(
    r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|FOREACH|GRANT|DENY|REVOKE|ALTER|RENAME|START|STOP|TERMINATE)\b",
    re.IGNORECASE,
)
_LOAD_CSV = re.compile(r"\bLOAD\s+CSV\b", re.IGNORECASE)
_IN_TX = re.compile(r"\bIN\s+(\d+\s+)?(CONCURRENT\s+)?TRANSACTIONS\b", re.IGNORECASE)
_CALL_PROC = re.compile(r"\bCALL\s+([A-Za-z_][\w.]*)", re.IGNORECASE)
_USE = re.compile(r"\bUSE\s+(system)\b", re.IGNORECASE)
# procedure namespaces that are allowed only when they are known read-only db.* procedures
_READ_ONLY_PROCS = {
    "db.labels", "db.relationshiptypes", "db.propertykeys", "db.schema.visualization",
    "db.schema.nodetypeproperties", "db.schema.reltypeproperties", "db.indexes", "db.constraints",
    "db.info", "db.ping",
}


class ReadOnlyViolation(ValueError):
    pass


def _strip_literals(query: str) -> str:
    """Blank out string literals, backticked identifiers and comments so keywords inside them
    (e.g. ``{text: 'set'}``) don't trigger false positives."""
    out: list[str] = []
    i, n = 0, len(query)
    while i < n:
        c = query[i]
        if c in ("'", '"', "`"):
            j = i + 1
            while j < n and query[j] != c:
                j += 2 if query[j] == "\\" and c != "`" else 1
            out.append(" ")
            i = j + 1
        elif query.startswith("//", i):
            j = query.find("\n", i)
            i = n if j < 0 else j
        elif query.startswith("/*", i):
            j = query.find("*/", i + 2)
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def check_read_only(query: str) -> None:
    """Raise ``ReadOnlyViolation`` if ``query`` could write. Conservative by design."""
    if not query or not query.strip():
        raise ReadOnlyViolation("empty query")
    q = _strip_literals(query)
    m = _WRITE_KEYWORDS.search(q)
    if m:
        raise ReadOnlyViolation(f"write clause '{m.group(1).upper()}' is not allowed (read-only)")
    if _LOAD_CSV.search(q):
        raise ReadOnlyViolation("LOAD CSV is not allowed (read-only)")
    if _IN_TX.search(q):
        raise ReadOnlyViolation("CALL ... IN TRANSACTIONS is not allowed (read-only)")
    if _USE.search(q):
        raise ReadOnlyViolation("USE system is not allowed")
    for m in _CALL_PROC.finditer(q):
        proc = m.group(1).lower()
        if proc.split(".")[0] in ("dbms", "apoc", "gds") or (proc.startswith("db.") and proc not in _READ_ONLY_PROCS):
            raise ReadOnlyViolation(f"procedure '{m.group(1)}' is not allowed (read-only)")


# --------------------------------------------------------------------------- runtime
_rt: Any = None


def _runtime() -> Any:
    global _rt
    if _rt is None:
        from backend import runtime as runtime_mod

        if runtime_mod._runtime is not None:
            _rt = runtime_mod._runtime
        elif os.environ.get("POLYTONE_GRAPH_STORE", "").lower() == "memory":
            from backend.graph.store import InMemoryGraphStore

            _rt = runtime_mod.build_runtime(store=InMemoryGraphStore(), scorer=False)
        else:
            _rt = runtime_mod.get_runtime()
    return _rt


def set_runtime(rt: Any) -> None:
    """Use an explicit Runtime (tests)."""
    global _rt
    _rt = rt


# --------------------------------------------------------------------------- tool impls
def execute_read_impl(query: str, params: dict[str, Any] | None = None, rt: Any = None) -> dict[str, Any]:
    try:
        check_read_only(query)
    except ReadOnlyViolation as e:
        return {"ok": False, "error": str(e), "rows": []}
    store = (rt or _runtime()).store
    fn = getattr(store, "execute_read", None)
    if not callable(fn):
        return {"ok": False, "error": f"execute_read needs Neo4j (graph store is '{store.kind}')", "rows": []}
    try:
        rows = fn(query, dict(params or {}))
    except Exception as e:  # noqa: BLE001 - surface driver errors (incl. Neo4j's own write refusal)
        return {"ok": False, "error": f"{e.__class__.__name__}: {e}", "rows": []}
    return {"ok": True, "rows": _jsonable(rows[:MAX_ROWS]), "truncated": len(rows) > MAX_ROWS}


def get_constraints_impl(persona: str | None = None, k: int = 20, rt: Any = None) -> dict[str, Any]:
    k = max(1, min(int(k or 20), 200))
    return dict((rt or _runtime()).store.get_constraints((persona or "").strip() or None, k))


def graph_stats_impl(rt: Any = None) -> dict[str, Any]:
    r = rt or _runtime()
    out = dict(r.store.stats())
    out["graph_store"] = r.store.kind
    out["samples_sqlite"] = r.db.count_samples()
    return out


def _jsonable(v: Any) -> Any:
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


# --------------------------------------------------------------------------- MCP server
mcp = make_server(
    "polytone-graph",
    "Read-only access to the Polytone style graph (Neo4j). Writes are rejected.",
)


@mcp.tool()
def execute_read(query: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run a read-only Cypher query against the style graph. Write clauses are rejected."""
    return execute_read_impl(query, params)


@mcp.tool()
def get_constraints(persona: str = "Default", k: int = 20) -> dict[str, Any]:
    """Top-k structural style constraints for a persona (same data /generate uses)."""
    return get_constraints_impl(persona, k)


@mcp.tool()
def graph_stats() -> dict[str, Any]:
    """Counts of words, structures and samples in the style graph."""
    return graph_stats_impl()


def main(argv: list[str] | None = None) -> None:
    del argv
    mcp.run("stdio")


if __name__ == "__main__":
    main(sys.argv[1:])
