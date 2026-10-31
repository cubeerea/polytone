"""HumanText provenance gate: only text the user actually wrote reaches the graph.

``check(text, source, provenance, db) -> (ok, reason)`` — ``reason`` is a human-readable
string whenever ``ok`` is False, else None. Checks run in this order:

1. empty / whitespace-only text                                  → "empty text"
2. provenance flagged ``generated=True`` / ``origin == "polytone"`` → "flagged as generated"
3. inbound mail (``direction == "inbound"`` / ``inbound=True``)    → "inbound mail is not ..."
4. per-source provenance (see ``SOURCE_RULES``):
   - gmail:   must be sent mail — ``from_me=True`` or a ``label``/``labels`` containing SENT
              (``label:sent``); ``from_me=False`` always rejects
   - watcher: ``.mbox`` items (``format == "mbox"`` or a ``path``/``file`` ending in .mbox)
              must carry ``from_me=True`` (message From: the user)
   - wispr:   must be the raw transcript — ``raw_transcript=True`` or ``text_kind == "raw"``;
              Wispr's AI-formatted/edited text (``formatted``/``edited``/``ai_edited``) is rejected
   - typed:   text the user typed and sent in a native app (typing capture) — needs
              ``typed=True``; ``after_inject=True`` (sent right after a Polytone inject) rejects
5. too short (< ``MIN_TOKENS`` word tokens or < ``MIN_CHARS`` characters)
6. duplicate of an existing sample (sha256)                       → "duplicate sample"
7. stored-draft checks (drafts NEVER reach the graph):
   - sha256 equal to a draft
   - a draft (≥ ``SUBSTRING_MIN_TOKENS`` tokens) contained in the text, or the text
     contained in a draft — compared on normalized token strings
   - 3-word shingle Jaccard ≥ ``NEAR_DUP_THRESHOLD`` with a draft
   - ≥ ``NEAR_DUP_THRESHOLD`` of a draft's shingles occur in the text (lightly edited draft
     pasted inside a longer message)

``db`` is a ``backend.storage.db.Database`` (needs ``draft_hashes()``, ``all_draft_texts()``,
``sample_exists(sha)``). Draft shingles are cached per draft-set.
"""
from __future__ import annotations

import threading
from typing import Any

from backend.ingestion.text import containment, jaccard, shingles, tokenize
from backend.storage.db import sha256_text

NEAR_DUP_THRESHOLD = 0.8
MIN_TOKENS = 3
MIN_CHARS = 10
SUBSTRING_MIN_TOKENS = 4

SOURCE_RULES = {
    "gmail": "sent mail only (from_me=True or label:sent)",
    "watcher": ".mbox messages only when From: the user (from_me=True)",
    "wispr": "raw transcripts only (raw_transcript=True or text_kind='raw')",
    "typed": "typed in a native app by the user (typed=True), never right after a Polytone inject",
}


def _truthy(v: Any) -> bool:
    return v is True or (isinstance(v, str) and v.strip().lower() in ("true", "1", "yes"))


def _labels(prov: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key in ("label", "labels", "label_ids", "labelIds", "query"):
        v = prov.get(key)
        if isinstance(v, str):
            out += v.replace(",", " ").split()
        elif isinstance(v, (list, tuple, set)):
            out += [str(x) for x in v]
    return [x.lower().removeprefix("label:").removeprefix("in:") for x in out]


def _check_source(source: str, prov: dict[str, Any]) -> str | None:
    src = (source or "").lower()
    if src == "gmail":
        if prov.get("from_me") is False:
            return "gmail message is not from the user (from_me=False)"
        if not (_truthy(prov.get("from_me")) or "sent" in _labels(prov)):
            return "gmail item lacks sent-mail provenance (needs from_me=True or label:sent)"
    elif src == "watcher":
        fmt = str(prov.get("format") or prov.get("kind") or "").lower()
        path = str(prov.get("path") or prov.get("file") or "").lower()
        is_mbox = fmt == "mbox" or path.endswith(".mbox")
        if is_mbox and not (_truthy(prov.get("from_me")) or _truthy(prov.get("from_user"))):
            return "mbox message is not From: the user (needs from_me=True)"
    elif src == "wispr":
        for flag in ("formatted", "edited", "ai_edited", "polished"):
            if _truthy(prov.get(flag)):
                return f"wispr text is {flag}, not a raw transcript"
        kind = str(prov.get("text_kind") or prov.get("kind") or "").lower()
        if not (_truthy(prov.get("raw_transcript")) or kind in ("raw", "raw_transcript")):
            return "wispr item is not marked as a raw transcript (needs raw_transcript=True)"
    elif src == "typed":
        if _truthy(prov.get("after_inject")):
            return "typed text was sent right after a Polytone inject (after_inject=True)"
        if not _truthy(prov.get("typed")):
            return "typed item lacks typing-capture provenance (needs typed=True)"
    return None


def _norm(text: str) -> str:
    return " " + " ".join(tokenize(text)) + " "


class _DraftIndex:
    """Normalized drafts + shingles, rebuilt only when the set of draft hashes changes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._key: tuple[int, frozenset[str]] | None = None
        self._rows: list[tuple[str, int, set]] = []

    def rows(self, db: Any, hashes: set[str]) -> list[tuple[str, int, set]]:
        key = (id(db), frozenset(hashes))
        with self._lock:
            if key != self._key:
                rows = []
                for d in db.all_draft_texts():
                    norm = _norm(d)
                    rows.append((norm, norm.count(" ") - 1, shingles(d)))
                self._rows, self._key = rows, key
            return self._rows


_INDEX = _DraftIndex()


def check(text: str, source: str, provenance: dict[str, Any] | None, db: Any) -> tuple[bool, str | None]:
    prov = provenance or {}
    if not text or not text.strip():
        return False, "empty text"
    if prov.get("generated") is True or prov.get("origin") == "polytone":
        return False, "flagged as generated"
    if prov.get("inbound") is True or str(prov.get("direction", "")).lower() == "inbound":
        return False, "inbound mail is not the user's writing"
    reason = _check_source(source, prov)
    if reason:
        return False, reason
    toks = tokenize(text)
    if len(toks) < MIN_TOKENS or len(text.strip()) < MIN_CHARS:
        return False, f"too short to learn from (need ≥ {MIN_TOKENS} words and ≥ {MIN_CHARS} characters)"
    sha = sha256_text(text)
    if db.sample_exists(sha):
        return False, "duplicate sample"
    reason = draft_match_reason(text, db, sha=sha)
    if reason:
        return False, reason
    return True, None


def draft_match_reason(text: str, db: Any, *, sha: str | None = None) -> str | None:
    """Why ``text`` looks like a stored Polytone draft (exact, substring either way, near-dup,
    or near-copy contained), or None. Used by ``check`` and by few-shot example selection."""
    hashes = db.draft_hashes()
    if not hashes:
        return None
    if (sha or sha256_text(text)) in hashes:
        return "matches a Polytone-generated draft"
    norm = _norm(text)
    n_toks = norm.count(" ") - 1
    sh = shingles(text)
    for d_norm, d_len, d_sh in _INDEX.rows(db, hashes):
        if d_len >= SUBSTRING_MIN_TOKENS and d_norm in norm:
            return "contains a Polytone-generated draft"
        if n_toks >= SUBSTRING_MIN_TOKENS and norm in d_norm:
            return "is an excerpt of a Polytone-generated draft"
        if jaccard(sh, d_sh) >= NEAR_DUP_THRESHOLD:
            return "near-duplicate of a Polytone-generated draft"
        if d_len >= SUBSTRING_MIN_TOKENS and containment(d_sh, sh) >= NEAR_DUP_THRESHOLD:
            return "contains a near-copy of a Polytone-generated draft"
    return None
