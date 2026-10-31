"""Retention purge and full wipe. See CONTRACTS.md → Retention and files.

- ``purge(rt)``: config ``retention_days`` (0 = keep forever) → delete ``typed``/``wispr``
  samples, ``draft_edits`` and ``drafts`` older than N days. The style graph keeps its aggregates.
  Runs at server start and then daily (``RetentionTask``). Audited as ``data.retention_purge``.
- ``wipe(rt, actor)``: delete every sample, draft, draft edit and the whole style graph, VACUUM
  the database and drop in-memory copies (decryption memo, few-shot caches). Audited as ``data.wipe``.
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
    from backend.security import crypto

    with examples._cache_lock:
        examples._cache.clear()
        examples._picks.clear()
    with crypto._dlock:
        crypto._dcache.clear()


class RetentionTask:
    """Purge now, then every ``interval_s`` until ``stop()``."""

    def __init__(self, rt: Any, interval_s: float = DAY_S) -> None:
        self.rt = rt
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="polytone-retention", daemon=True)

    def start(self) -> RetentionTask:
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                purge(self.rt)
            except Exception as e:  # noqa: BLE001 - try again next round
                log.warning("retention purge failed: %s", e.__class__.__name__)
            self._stop.wait(self.interval_s)

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
