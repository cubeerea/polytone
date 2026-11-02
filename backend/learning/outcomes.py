"""Draft outcomes: what happened to a draft after Polytone showed it (``drafts.outcome``).

- ``accepted``    the draft was injected into the field (Rust, after a successful paste/type) —
                  ``POST /drafts/{id}/outcome``.
- ``regenerated`` the user ran the hotkey again on the injected draft: the new intent *is* (almost
                  all of) a draft of the last ``REGEN_WINDOW_S``. Detected here, off the request
                  thread, on every generation.
- ``discarded``   the draft was thrown away (accepted by the endpoint; no client sends it yet).
- ``edited``      typing capture saw the user send an edited version (``ingestion/sources.py``).
                  A send identical to the draft keeps ``accepted``.

Outcomes feed the few-shot ranking (``langgraph/examples.py``: structures of drafts the user kept
get a bounded boost). They are labels, not text: nothing here is learned into the graph.
"""
from __future__ import annotations

import logging
from typing import Any

from backend.ingestion.text import containment, shingles

log = logging.getLogger(__name__)

REGEN_WINDOW_S = 600  # same window as the post-inject edit match
REGEN_MIN = 0.8  # the intent must contain ≥80% of the draft's shingles, and vice versa


def note_regeneration(db: Any, intent: str, new_draft_id: str | None) -> str | None:
    """Mark the recent draft this intent re-runs as ``regenerated``. Returns its id, or None."""
    sent = shingles(intent)
    if len(sent) < 2:
        return None
    best: tuple[str, float] | None = None
    for d in db.drafts_within(REGEN_WINDOW_S):
        if d["draft_id"] == new_draft_id:
            continue
        ds = shingles(d["text"])
        score = min(containment(ds, sent), containment(sent, ds))
        if score >= REGEN_MIN and (best is None or score > best[1]):
            best = (d["draft_id"], score)
    if best is None:
        return None
    db.set_draft_outcome(best[0], "regenerated")
    return best[0]


def note_regeneration_safe(db: Any, intent: str, new_draft_id: str | None) -> None:
    try:
        note_regeneration(db, intent, new_draft_id)
    except Exception as e:  # noqa: BLE001 - a label; never fail anything over it
        log.warning("regeneration check failed: %s", type(e).__name__)


def edit_outcome(draft_text: str, sent: str) -> str:
    """``edited`` unless the user sent the draft as it was (whitespace aside)."""
    return "accepted" if " ".join(draft_text.split()) == " ".join(sent.split()) else "edited"
