"""Process-wide wiring of settings, SQLite, graph store and LLM.

Everything that needs dependencies (LangGraph nodes, FastAPI routes, MCP servers) takes a
``Runtime``. Tests build one explicitly with ``build_runtime(db_path=..., store=..., llm=...)``
and install it with ``set_runtime``; production calls ``get_runtime()``, which lazily builds
the default (Neo4j if reachable, else in-memory).
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.config import Settings, get_settings
from backend.graph.store import GraphStore, connect_graph_store
from backend.llm.client import LLM, get_llm
from backend.security.audit import AuditLog
from backend.storage.db import Database

log = logging.getLogger(__name__)

AUDIT_FILE = "audit.sqlite3"

# scorer(text, baseline_texts, persona) -> ScoreBreakdown
Scorer = Callable[[str, list[str], str | None], dict]


def load_scorer() -> Scorer | None:
    """``backend.evaluation.style_distance.score`` if implemented, else None."""
    try:
        from backend.evaluation import style_distance  # type: ignore[attr-defined]
    except ImportError:
        return None
    fn = getattr(style_distance, "score", None)
    return fn if callable(fn) else None


@dataclass
class Runtime:
    settings: Settings
    db: Database
    store: GraphStore
    llm: LLM
    # None → look up backend.evaluation.style_distance.score at call time; False → disabled
    scorer: Scorer | None | bool = None
    # background work allowed (the server): the score node and constraint-cache priming run on a
    # worker thread so they never delay /generate; tests keep everything synchronous
    score_async: bool = False
    # tamper-evident audit log (backend/security/audit.py); None → events are dropped
    audit: AuditLog | None = None
    _graphs: dict[str, Any] = field(default_factory=dict, repr=False)
    _executor: ThreadPoolExecutor | None = field(default=None, repr=False)

    def get_scorer(self) -> Scorer | None:
        if self.scorer is False:
            return None
        if callable(self.scorer):
            return self.scorer
        return load_scorer()

    def prime_constraints(self) -> None:
        """Compute (and cache) the active persona's constraints so the next /generate hits the cache."""
        persona = self.db.get_config().get("active_persona") or "Default"
        try:
            self.store.get_constraints(persona)
        except Exception as e:  # noqa: BLE001 - generation falls back to empty constraints anyway
            log.warning("constraint warm-up failed: %s", type(e).__name__)

    def audit_event(self, event: str, detail: dict[str, Any] | None = None, actor: str = "backend") -> None:
        """Append an audit event (never user text: counts, ids, lengths, bundle ids)."""
        if self.audit is not None:
            self.audit.append(event, detail or {}, actor)

    def audit_event_soon(self, event: str, detail: dict[str, Any] | None = None, actor: str = "backend") -> None:
        """Like ``audit_event`` but off the request thread in the server (``score_async``)."""
        if self.score_async:
            self.submit(self.audit_event, event, detail, actor)
        else:
            self.audit_event(event, detail, actor)

    def submit(self, fn: Callable[..., Any], *args: Any) -> None:
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="polytone-score")
        self._executor.submit(fn, *args)

    def close(self) -> None:
        if self._executor is not None:
            self._executor.shutdown(wait=True)
            self._executor = None
        try:
            self.store.close()
        except Exception as e:  # noqa: BLE001
            log.debug("closing graph store failed: %s", e)
        if self.audit is not None:
            self.audit.close()


def build_runtime(
    settings: Settings | None = None,
    *,
    db_path: str | Path | None = None,
    store: GraphStore | None = None,
    llm: LLM | None = None,
    scorer: Scorer | None | bool = None,
    score_async: bool = False,
) -> Runtime:
    from backend.security import keys

    settings = settings or get_settings()
    keys.master_key()  # read (or create) the data key now: fail at start, not on the first write
    audit = AuditLog(Path(settings.polytone_data_dir) / AUDIT_FILE)
    db = Database(db_path or settings.sqlite_path)  # migrates plaintext rows (creates the key if new)
    rt = Runtime(
        settings=settings,
        db=db,
        store=store if store is not None else connect_graph_store(settings),
        llm=llm if llm is not None else get_llm(settings),
        scorer=scorer,
        score_async=score_async,
        audit=audit,
    )
    if keys.consume_created():
        rt.audit_event("key.created", {"service": keys.KEYCHAIN_SERVICE, "account": keys.KEYCHAIN_ACCOUNT})
    graph_migration = getattr(rt.store, "migration", None) or {}
    if db.migration or graph_migration:
        rt.audit_event("migration.encrypted", {"sqlite": dict(db.migration), "neo4j": dict(graph_migration)})
    return rt


_runtime: Runtime | None = None
_lock = threading.Lock()


def get_runtime() -> Runtime:
    global _runtime
    with _lock:
        if _runtime is None:
            _runtime = build_runtime()
        return _runtime


def set_runtime(rt: Runtime | None) -> None:
    global _runtime
    with _lock:
        _runtime = rt
