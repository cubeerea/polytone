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
from backend.storage.db import Database

log = logging.getLogger(__name__)

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
    # run the score node in a background thread so it never delays /generate
    score_async: bool = False
    _graphs: dict[str, Any] = field(default_factory=dict, repr=False)
    _executor: ThreadPoolExecutor | None = field(default=None, repr=False)

    def get_scorer(self) -> Scorer | None:
        if self.scorer is False:
            return None
        if callable(self.scorer):
            return self.scorer
        return load_scorer()

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


def build_runtime(
    settings: Settings | None = None,
    *,
    db_path: str | Path | None = None,
    store: GraphStore | None = None,
    llm: LLM | None = None,
    scorer: Scorer | None | bool = None,
    score_async: bool = False,
) -> Runtime:
    settings = settings or get_settings()
    db = Database(db_path or settings.sqlite_path)
    return Runtime(
        settings=settings,
        db=db,
        store=store if store is not None else connect_graph_store(settings),
        llm=llm if llm is not None else get_llm(settings),
        scorer=scorer,
        score_async=score_async,
    )


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
