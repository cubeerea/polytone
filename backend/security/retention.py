"""Retention purge and full wipe. See CONTRACTS.md → Retention and files.

- ``purge(rt)``: config ``retention_days`` (0 = keep forever) → delete ``typed``/``wispr``
  samples, ``draft_edits`` and ``drafts`` older than N days. The style graph keeps its aggregates,
  and so do learning evaluations (``constraint_snapshots``: derived constraints like the graph's,
  encrypted at rest; their scores/decisions are numbers). Only a wipe or a persona delete removes them.
  Runs at server start and then daily (``RetentionTask``). Audited as ``data.retention_purge``.
- ``graph_maintenance(rt)``: the style graph's recency decay upkeep (``GraphStore.decay_maintenance``:
  rebase the epoch when due, prune edges that faded away; ``graph/decay.py``). Same schedule,
  audited as ``graph.decay_maintenance`` with counts only, when it changed something.
- ``wipe(rt, actor)``: delete every sample, draft, draft edit, edit correction, learning evaluation
  and override and the whole style graph, VACUUM the database and drop in-memory copies (decryption
  memo, few-shot caches, learning-state cache). Audited as ``data.wipe``.
"""
from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)

DAY_S = 24 * 3600
MAX_RETENTION_DAYS = 3650


def retention_days(db: Any) -> int:
    try:
        return max(0, min(MAX_RETENTION_DAYS, int(db.get_config().get("retention_days") or 0)))
    except (TypeError, ValueError):
        return 0


def purge(rt: Any, now: datetime | None = None) -> dict[str, int]:
    days = retention_days(rt.db)
    if days <= 0:
        return {}
    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    out = rt.db.purge_older_than(cutoff.isoformat(timespec="milliseconds"))
    if any(out.values()):
        _forget_memory()
        rt.audit_event("data.retention_purge", {"retention_days": days, **out})
        log.info("retention purge (%d days): %s", days, out)
    return out


def wipe(rt: Any, actor: str = "ui") -> dict[str, int]:
    out = dict(rt.db.wipe_user_data())
    try:
        out["graph_nodes"] = int(rt.store.wipe())
    except Exception as e:  # noqa: BLE001 - SQLite is wiped either way; report the graph failure
        log.warning("graph wipe failed: %s", e.__class__.__name__)
        out["graph_nodes"] = -1
    _forget_memory()
    rt.audit_event("data.wipe", out, actor)
    return out


def _forget_memory() -> None:
    from backend.langgraph import examples
    from backend.learning import edits
    from backend.learning import state as learning_state
    from backend.security import crypto

    edits.invalidate()
    learning_state.invalidate()

    with examples._cache_lock:
        examples._cache.clear()
        examples._picks.clear()
        examples._kept.clear()
    with crypto._dlock:
        crypto._dcache.clear()


def graph_maintenance(rt: Any) -> dict[str, int]:
    """Run the graph's decay maintenance; never raises (the server keeps running)."""
    try:
        out = dict(rt.store.decay_maintenance() or {})
    except Exception as e:  # noqa: BLE001 - e.g. Neo4j went away; try again next round
        log.warning("graph decay maintenance failed: %s", e.__class__.__name__)
        return {}
    if any(out.values()):
        half_life = getattr(getattr(rt.store, "decay", None), "half_life_days", 0)
        rt.audit_event("graph.decay_maintenance", {"half_life_days": half_life, **out})
        log.info("graph decay maintenance: %s", out)
    return out


class RetentionTask:
    """Purge and graph maintenance now, then every ``interval_s`` until ``stop()``."""

    def __init__(self, rt: Any, interval_s: float = DAY_S) -> None:
        self.rt = rt
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="polytone-retention", daemon=True)

    def start(self) -> RetentionTask:
        self._thread.start()
        return self

    def _run(self) -> None:
        while True:  # the first round always runs, even when stop() came right after start()
            try:
                purge(self.rt)
            except Exception as e:  # noqa: BLE001 - try again next round
                log.warning("retention purge failed: %s", e.__class__.__name__)
            graph_maintenance(self.rt)
            if self._stop.wait(self.interval_s):
                break

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
