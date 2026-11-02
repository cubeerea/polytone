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
              Wispr's AI-formatted/edited text (``formatted``/``edited``/``ai_edited``) is rejected;
              and the dictation's ``app`` must be in the Learning app list (``capture_apps``, the
              same list typing capture uses — see ``app_policy``); no recorded app rejects
   - typed:   text the user typed and sent in a native app (typing capture) — needs
              ``typed=True``; ``after_inject=True`` (sent right after a Polytone inject) rejects
5. too short (< ``MIN_TOKENS`` word tokens or < ``MIN_CHARS`` characters)
6. duplicate of an existing sample (blind index)                   → "duplicate sample"
7. stored-draft checks (drafts NEVER reach the graph):
   - blind index (keyed hash) equal to a draft
   - a draft (≥ ``SUBSTRING_MIN_TOKENS`` tokens) contained in the text, or the text
     contained in a draft — compared on normalized token strings
   - 3-word shingle Jaccard ≥ ``NEAR_DUP_THRESHOLD`` with a draft
   - ≥ ``NEAR_DUP_THRESHOLD`` of a draft's shingles occur in the text (lightly edited draft
     pasted inside a longer message)

``db`` is a ``backend.storage.db.Database`` (needs ``draft_watermark()``, ``drafts_since(rowid)``,
``sample_exists(sha)``).

Draft index: per database file the normalized drafts, their shingles and an inverted index
shingle → drafts are kept in memory and synced incrementally (one ``COUNT/MAX(rowid)`` query per
check; only new rows are fetched; any deletion triggers a full rebuild), so the check never
misses a draft written by another process. Only drafts sharing at least one shingle with the
text are compared: every rule above implies a shared shingle (substring either way of ≥ 3
tokens shares all its 3-grams; Jaccard / containment ≥ 0.8 need a non-empty intersection;
texts under 3 tokens are a single shingle compared exactly), so the verdict is identical to a
full scan — candidates are visited in insertion order, so the reason string is too.
"""
from __future__ import annotations

import threading
from typing import Any

from backend.ingestion import app_policy
from backend.ingestion.text import containment, jaccard, shingles, tokenize
from backend.storage.db import text_index

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


def _check_source(source: str, prov: dict[str, Any], db: Any) -> str | None:
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
        why = app_policy.skip_reason(prov.get("app"), app_policy.allowed_apps(db))
        if why:
            return f"wispr {why}"
    elif src == "typed":
        if _truthy(prov.get("after_inject")):
            return "typed text was sent right after a Polytone inject (after_inject=True)"
        if not _truthy(prov.get("typed")):
            return "typed item lacks typing-capture provenance (needs typed=True)"
    return None


def _norm(text: str) -> str:
    return " " + " ".join(tokenize(text)) + " "


class DraftSnapshot:
    """View of the first ``n`` indexed drafts. The index only ever appends to the structures a
    snapshot references (a rebuild swaps in new ones), so it is safe to read without a lock."""

    __slots__ = ("hash_pos", "n", "postings", "rows")

    def __init__(self, rows: list, postings: dict, hash_pos: dict, n: int) -> None:
        self.rows = rows  # (normalized text, token count, shingles, sha256), insertion order
        self.postings = postings  # shingle -> [row positions], ascending
        self.hash_pos = hash_pos  # sha256 -> first row position
        self.n = n

    def has_hash(self, sha: str, start: int = 0) -> bool:
        if start:  # only drafts at positions >= start (few: those added since a cached check)
            return any(self.rows[p][3] == sha for p in range(start, self.n))
        pos = self.hash_pos.get(sha)
        return pos is not None and pos < self.n

    def candidates(self, sh: set, start: int = 0) -> list[int]:
        found: set[int] = set()
        for g in sh:
            found.update(self.postings.get(g, ()))
        return sorted(p for p in found if start <= p < self.n)


class _DraftIndex:
    """One per database file; see the module docstring."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reset()

    def _reset(self) -> None:
        self._mark: tuple[int, int, str] = (0, 0, "")  # (rows indexed, max rowid indexed, its draft_id)
        self._rows: list[tuple[str, int, frozenset, str]] = []
        self._postings: dict[tuple, list[int]] = {}
        self._hash_pos: dict[str, int] = {}

    def _add(self, sha: str, text: str) -> None:
        norm = _norm(text)
        sh = frozenset(shingles(text))
        pos = len(self._rows)
        self._rows.append((norm, norm.count(" ") - 1, sh, sha))
        self._hash_pos.setdefault(sha, pos)
        for g in sh:
            self._postings.setdefault(g, []).append(pos)

    def snapshot(self, db: Any) -> DraftSnapshot:
        count, top, top_id = db.draft_watermark()
        with self._lock:
            if (count, top, top_id) != self._mark:
                last = self._mark[1]
                rebuild = count < self._mark[0] or top < last  # drafts were deleted
                new = db.drafts_since(last - 1) if last and not rebuild else []
                if last and not rebuild:
                    # SQLite reuses deleted rowids (e.g. after a wipe): the newest draft indexed
                    # must still be there, else the rows below it may be different drafts now
                    if new and new[0][0] == last and new[0][3] == self._mark[2]:
                        new = new[1:]
                    else:
                        rebuild = True
                if rebuild or not last:
                    self._reset()
                    new = db.drafts_since(0)
                for _, sha, text, _id in new:
                    self._add(sha, text)
                if new:
                    self._mark = (len(self._rows), new[-1][0], new[-1][3])
            return DraftSnapshot(self._rows, self._postings, self._hash_pos, len(self._rows))


_INDEXES: dict[str, _DraftIndex] = {}
_INDEXES_LOCK = threading.Lock()


def draft_snapshot(db: Any) -> DraftSnapshot:
    """Current drafts of ``db`` (synced incrementally). Reuse one snapshot for many checks."""
    key = str(getattr(db, "path", id(db)))
    with _INDEXES_LOCK:
        idx = _INDEXES.get(key)
        if idx is None:
            idx = _INDEXES[key] = _DraftIndex()
    return idx.snapshot(db)


def check(text: str, source: str, provenance: dict[str, Any] | None, db: Any) -> tuple[bool, str | None]:
    prov = provenance or {}
    if not text or not text.strip():
        return False, "empty text"
    if prov.get("generated") is True or prov.get("origin") == "polytone":
        return False, "flagged as generated"
    if prov.get("inbound") is True or str(prov.get("direction", "")).lower() == "inbound":
        return False, "inbound mail is not the user's writing"
    reason = _check_source(source, prov, db)
    if reason:
        return False, reason
    toks = tokenize(text)
    if len(toks) < MIN_TOKENS or len(text.strip()) < MIN_CHARS:
        return False, f"too short to learn from (need ≥ {MIN_TOKENS} words and ≥ {MIN_CHARS} characters)"
    sha = text_index(text)
    if db.sample_exists(sha):
        return False, "duplicate sample"
    reason = draft_match_reason(text, db, sha=sha)
    if reason:
        return False, reason
    return True, None


def draft_match_reason(
    text: str, db: Any, *, sha: str | None = None, snapshot: DraftSnapshot | None = None, start: int = 0
) -> str | None:
    """Why ``text`` looks like a stored Polytone draft (exact, substring either way, near-dup,
    or near-copy contained), or None. Used by ``check`` and by few-shot example selection
    (which passes one ``snapshot`` for all its candidates, and ``start`` to re-check a cached
    pick against only the drafts indexed at positions >= ``start``)."""
    snap = snapshot if snapshot is not None else draft_snapshot(db)
    if snap.n <= start:
        return None
    if snap.has_hash(sha or text_index(text), start):
        return "matches a Polytone-generated draft"
    norm = _norm(text)
    n_toks = norm.count(" ") - 1
    sh = shingles(text)
    for pos in snap.candidates(sh, start):
        d_norm, d_len, d_sh, _ = snap.rows[pos]
        if d_len >= SUBSTRING_MIN_TOKENS and d_norm in norm:
            return "contains a Polytone-generated draft"
        if n_toks >= SUBSTRING_MIN_TOKENS and norm in d_norm:
            return "is an excerpt of a Polytone-generated draft"
        if jaccard(sh, d_sh) >= NEAR_DUP_THRESHOLD:
            return "near-duplicate of a Polytone-generated draft"
        if d_len >= SUBSTRING_MIN_TOKENS and containment(d_sh, sh) >= NEAR_DUP_THRESHOLD:
            return "contains a near-copy of a Polytone-generated draft"
    return None
