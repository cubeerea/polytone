"""Which constraints generation uses for a persona, after the last learning evaluation.

An evaluation (``backend/learning/evaluate.py``) can leave a per-persona override in the
``learning_state`` table until the next evaluation (or until the user clears it):

- ``pinned``             generation uses the constraints of snapshot ``snapshot_id`` (the last
                         accepted one) instead of graph + edit corrections: learning since then made
                         drafts sound less like the user.
- ``corrections_paused`` generation uses the graph constraints without the edit-correction layer.
- no row ("normal")      graph constraints with corrections applied (``edits.corrected``).

The state is read once per persona and cached in process (generation latency is unchanged: a
dict lookup per draft); ``invalidate`` drops it whenever an evaluation, a clear, a persona delete
or a wipe changes it. Anything unreadable (corrupt row, snapshot that no longer decrypts, missing
snapshot) degrades to "normal" with a warning: an override is an optimisation, never a reason for a
draft to fail.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from backend.learning.edits import corrected

log = logging.getLogger(__name__)

NORMAL, PINNED, PAUSED = "normal", "pinned", "corrections_paused"

# key -> (mode, pinned constraints | None)
_CACHE: dict[str, tuple[str, dict[str, Any] | None]] = {}
_LOCK = threading.Lock()


def _key(db: Any, persona: str) -> str:
    return f"{getattr(db, 'path', id(db))}\x00{persona.lower()}"


def invalidate(persona: str | None = None) -> None:
    with _LOCK:
        if persona is None:
            _CACHE.clear()
            return
        for k in [k for k in _CACHE if k.endswith("\x00" + persona.lower())]:
            del _CACHE[k]


def _load(db: Any, persona: str) -> tuple[str, dict[str, Any] | None]:
    try:
        row = db.get_learning_state(persona)
        if row is None:
            return NORMAL, None
        mode = row.get("mode")
        if mode == PAUSED:
            return PAUSED, None
        if mode == PINNED:
            snap = db.get_snapshot(int(row["snapshot_id"]))
            c = (snap or {}).get("constraints")
            if isinstance(c, dict) and c:
                return PINNED, c
            log.warning("pinned learning snapshot is missing or unreadable; using live constraints")
            return NORMAL, None
        log.warning("unknown learning mode; using live constraints")
    except Exception as e:  # noqa: BLE001 - never break generation over an override
        log.warning("learning state unreadable (%s); using live constraints", type(e).__name__)
    return NORMAL, None


def mode(db: Any, persona: str) -> tuple[str, dict[str, Any] | None]:
    """``(mode, pinned constraints | None)`` for ``persona`` (cached)."""
    key = _key(db, persona)
    with _LOCK:
        hit = _CACHE.get(key)
    if hit is None:
        hit = _load(db, persona)
        with _LOCK:
            _CACHE[key] = hit
    return hit


def effective(db: Any, persona: str, graph_constraints: dict[str, Any]) -> dict[str, Any]:
    """The constraints generation should use for ``persona`` given its graph constraints."""
    m, pinned = mode(db, persona)
    if m == PINNED and pinned is not None:
        return dict(pinned)
    if m == PAUSED:
        return graph_constraints
    return corrected(db, persona, graph_constraints)
