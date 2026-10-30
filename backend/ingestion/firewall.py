"""HumanText provenance gate: only text the user actually wrote reaches the graph.

``check(text, source, provenance, db) -> (ok, reason)``

Rejects:
- empty / whitespace-only text,
- provenance flagged ``generated=True`` (or ``origin == "polytone"``),
- inbound mail (``direction == "inbound"`` or ``inbound=True``),
- text whose sha256 matches a stored draft,
- near-duplicates of a stored draft (3-word shingle Jaccard >= 0.8).

``db`` is a ``backend.storage.db.Database`` (anything with ``draft_hashes()`` and
``all_draft_texts()``). A later phase may extend this; keep the signature.
"""
from __future__ import annotations

from typing import Any

from backend.ingestion.text import jaccard, shingles
from backend.storage.db import sha256_text

NEAR_DUP_THRESHOLD = 0.8


def check(text: str, source: str, provenance: dict[str, Any] | None, db: Any) -> tuple[bool, str | None]:
    prov = provenance or {}
    if not text or not text.strip():
        return False, "empty text"
    if prov.get("generated") is True or prov.get("origin") == "polytone":
        return False, "flagged as generated"
    if prov.get("inbound") is True or str(prov.get("direction", "")).lower() == "inbound":
        return False, "inbound mail is not the user's writing"
    if sha256_text(text) in db.draft_hashes():
        return False, "matches a Polytone-generated draft"
    sh = shingles(text)
    if sh:
        for draft in db.all_draft_texts():
            if jaccard(sh, shingles(draft)) >= NEAR_DUP_THRESHOLD:
                return False, "near-duplicate of a Polytone-generated draft"
    return True, None
