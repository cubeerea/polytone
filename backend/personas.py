"""Personas: the four built-ins plus any the user adds.

A persona is a name and a one-line tone description. The description goes into the system
prompt (``prompts.build_system_parts``); the name is also what samples and the style graph are
attributed to, so a persona's own writing shapes its drafts as samples accumulate.

Storage: the built-ins live in code (``prompts.PERSONAS``). The ``personas`` table holds the
user's custom personas and the user's own wording for a built-in (an "override"). A name that
only appears on samples (learned under a name typed in before personas could be managed) is
listed as a custom persona with a generic description until the user gives it one.

Deleting a custom persona moves its samples, and everything the graph learned from them, to
Default — nothing the user wrote is lost. Built-ins can be reworded or reset, never deleted.
"""
from __future__ import annotations

import logging
import re
import threading
from typing import Any

from backend.langgraph.prompts import PERSONAS as BUILTIN

log = logging.getLogger(__name__)

DEFAULT = "Default"
NAME_MAX = 30
DESCRIPTION_MAX = 300
MAX_CUSTOM = 24
_NAME_RE = re.compile(r"^[^\W_](?:[\w '&().+-]*[\w).+])?$")


class _ReadWriteLock:
    """Many readers (ingests) or one writer (a persona change). Writers wait for readers to drain
    and block new ones, so a sample is never half-written while its persona is being deleted."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._readers = 0
        self._writer = False
        self._local = threading.local()

    def _owns_write(self) -> bool:
        return getattr(self._local, "writing", False)

    def read(self) -> Any:
        from contextlib import contextmanager

        @contextmanager
        def ctx():
            if self._owns_write():  # a writer may ingest (nested) without deadlocking
                yield
                return
            with self._cond:
                while self._writer:
                    self._cond.wait()
                self._readers += 1
            try:
                yield
            finally:
                with self._cond:
                    self._readers -= 1
                    self._cond.notify_all()

        return ctx()

    def write(self) -> Any:
        from contextlib import contextmanager

        @contextmanager
        def ctx():
            with self._cond:
                while self._writer or self._readers:
                    self._cond.wait()
                self._writer = True
            self._local.writing = True
            try:
                yield
            finally:
                self._local.writing = False
                with self._cond:
                    self._writer = False
                    self._cond.notify_all()

        return ctx()


# Ingest holds ``lock.read()`` around resolving the persona name and writing the sample; creating
# and deleting personas hold ``lock.write()``.
lock = _ReadWriteLock()


class PersonaError(ValueError):
    """A request the user can fix (bad name, duplicate, unknown persona, built-in delete)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class GraphUnavailable(PersonaError):
    """The style graph could not be updated; nothing was changed."""

    def __init__(self) -> None:
        super().__init__("Couldn't reach the style graph, so nothing was deleted. Start Neo4j and try again.", 503)


def _fallback(name: str) -> str:
    return f"Writing as the user's '{name}' persona; match that relationship's tone."


def clean_name(name: str) -> str:
    n = " ".join((name or "").split())
    if not n:
        raise PersonaError("Give the persona a name.")
    if len(n) > NAME_MAX:
        raise PersonaError(f"Keep the name to {NAME_MAX} characters or fewer.")
    if not _NAME_RE.match(n):
        raise PersonaError("Use letters, numbers, spaces and simple punctuation (- ' & . ( ) +) in the name.")
    return n


def clean_description(description: str) -> str:
    d = " ".join((description or "").split())
    if not d:
        raise PersonaError("Describe the tone in a few words, e.g. “warm, brief, ends with a next step”.")
    if len(d) > DESCRIPTION_MAX:
        raise PersonaError(f"Keep the tone description to {DESCRIPTION_MAX} characters or fewer.")
    return d


def _builtin_name(name: str) -> str | None:
    return next((b for b in BUILTIN if b.lower() == name.lower()), None)


def list_personas(db: Any) -> list[dict[str, Any]]:
    """Every persona, built-ins first (in their usual order), then the user's in creation order.
    ``{name, description, builtin, edited, samples}``."""
    rows = db.list_persona_rows()
    counts = db.count_samples_by_persona()
    by_lower = {r["name"].lower(): r for r in rows}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name, desc in BUILTIN.items():
        row = by_lower.get(name.lower())
        out.append({"name": name, "description": row["description"] if row and row["description"] else desc,
                    "builtin": True, "edited": bool(row and row["description"]), "samples": counts.get(name, 0)})
        seen.add(name.lower())
    for r in rows:
        if r["name"].lower() in seen:
            continue
        out.append({"name": r["name"], "description": r["description"] or _fallback(r["name"]), "builtin": False,
                    "edited": True, "samples": counts.get(r["name"], 0)})
        seen.add(r["name"].lower())
    for name in sorted(counts):
        if name.lower() not in seen:
            out.append({"name": name, "description": f"Custom persona learned from your '{name}' samples.",
                        "builtin": False, "edited": False, "samples": counts[name]})
            seen.add(name.lower())
    return out


def canonical(db: Any, name: str | None) -> str:
    """The stored spelling of ``name`` when a persona matches it case-insensitively ("boss" → "Boss"),
    else ``name`` itself (whitespace-normalized). Cheap: built-ins, the personas table and the
    distinct sample personas (indexed) — it runs on every ingest and draft."""
    n = " ".join((name or "").split())
    if not n:
        return n
    low = n.lower()
    hit = _builtin_name(n)
    if hit:
        return hit
    try:
        for other in [r["name"] for r in db.list_persona_rows()] + list(db.sample_personas()):
            if other.lower() == low:
                return other
    except Exception as e:  # noqa: BLE001 - never block a draft or an ingest on this lookup
        log.warning("persona lookup failed: %s", type(e).__name__)
    return n


def find(db: Any, name: str) -> dict[str, Any] | None:
    n = " ".join((name or "").split()).lower()
    return next((p for p in list_personas(db) if p["name"].lower() == n), None)


def guidance(db: Any, name: str) -> str:
    """The tone line for the system prompt: the user's wording, else the built-in's, else generic."""
    n = " ".join((name or "").split()).lower()
    try:  # only the small personas table: this runs on every draft
        row = next((r for r in db.list_persona_rows() if r["name"].lower() == n), None)
    except Exception:  # noqa: BLE001 - the prompt must still build if the table is unreadable
        row = None
    if row and row["description"]:
        return row["description"]
    return BUILTIN.get(_builtin_name(name) or "", "") or _fallback(name)


def create(db: Any, name: str, description: str) -> dict[str, Any]:
    with lock.write():
        return _create(db, name, description)


def _create(db: Any, name: str, description: str) -> dict[str, Any]:
    n, d = clean_name(name), clean_description(description)
    existing = find(db, n)
    if existing and (existing["builtin"] or existing["edited"]):
        raise PersonaError(f"There's already a persona called “{existing['name']}”.", 409)
    if existing:  # samples already learned under this name: adopt it, keep its spelling
        n = existing["name"]
    elif sum(1 for p in list_personas(db) if not p["builtin"]) >= MAX_CUSTOM:
        raise PersonaError(f"You can have up to {MAX_CUSTOM} of your own personas. Delete one first.", 409)
    db.upsert_persona_row(n, d)
    return find(db, n)  # type: ignore[return-value]


def update(db: Any, name: str, description: str | None) -> dict[str, Any]:
    """Reword a persona's tone. ``None`` on a built-in resets it to Polytone's wording."""
    p = find(db, name)
    if p is None:
        raise PersonaError(f"No persona called “{name}”.", 404)
    if description is None:
        if not p["builtin"]:
            raise PersonaError("Only built-in personas can be reset.")
        db.delete_persona_row(p["name"])
    else:
        db.upsert_persona_row(p["name"], clean_description(description))
    return find(db, p["name"])  # type: ignore[return-value]


def delete(rt: Any, name: str) -> dict[str, Any]:
    """Delete a custom persona: its samples and graph learning move to Default, and Default becomes
    active if it was. Returns ``{"name", "moved_samples", "active_persona"}``."""
    from backend.langgraph import examples

    with lock.write():  # no ingest runs (or starts) until the move is complete
        p = find(rt.db, name)
        if p is None:
            raise PersonaError(f"No persona called “{name}”.", 404)
        if p["builtin"]:
            raise PersonaError(f"“{p['name']}” is built in and can't be deleted. You can reword its tone.")
        n = p["name"]
        if getattr(rt.store, "fallback", False):
            # Neo4j was down at start-up: the real graph would keep this persona forever
            raise GraphUnavailable()
        active = rt.db.get_config().get("active_persona") or DEFAULT
        prev_active = active
        if active.lower() == n.lower():
            active = DEFAULT
            rt.db.set_config(active_persona=DEFAULT)
        # graph first: if Neo4j is down nothing has changed yet and the user can retry
        try:
            rt.store.merge_persona(n, DEFAULT)
        except Exception as e:
            if prev_active != active:
                rt.db.set_config(active_persona=prev_active)
            raise GraphUnavailable() from e
        moved = rt.db.move_samples(n, DEFAULT)
        rt.db.delete_persona_row(n)
        examples.invalidate()  # cached picks are keyed on a samples watermark a move doesn't change
        return {"name": n, "moved_samples": moved, "active_persona": active}
