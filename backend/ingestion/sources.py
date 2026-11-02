"""Ingestion sources registry used by ``/ingest/sources`` and ``/ingest/sources/{name}/run``.

Each non-manual source maps to a module in ``backend.mcp_servers``. A source module may expose
(all optional; a missing module or missing ``collect`` makes the source "unavailable"):

    def collect(settings) -> Iterable[dict | str]
        # yields human text items: {"text": str, "provenance": dict?, "persona": str?}
        # provenance should carry e.g. {"direction": "outbound"} / {"generated": False}
    def is_enabled(settings) -> bool          # default: True when collect exists
    def status(settings) -> str               # default: last run status or "ready"

Collected items still pass through the full ingest graph (validate → firewall → ...).

``typed`` is push-only: the Tauri shell's typing capture POSTs each message the user sends in an
allowlisted app to ``/ingest/typed`` (→ ``ingest_typed``). It has nothing to collect, so running it
is refused; it is "enabled" exactly when ``capture_enabled`` is set in ``/config``. Text Wispr Flow
inserted into the message (its AI-formatted dictation) is removed first; when what is left would
not pass the firewall on its own the message is rejected as ``wispr_inserted``.
"""
from __future__ import annotations

import importlib
import logging
from types import ModuleType
from typing import Any

from backend.runtime import Runtime

log = logging.getLogger(__name__)

SOURCES: tuple[str, ...] = ("manual", "gmail", "wispr", "watcher", "typed")
PUSH_ONLY: frozenset[str] = frozenset({"typed"})
TYPED_MAX_CHARS = 5000
AFTER_INJECT_REASON = "after_polytone_inject: stored for edit learning"
AFTER_INJECT_LEARNED = "after_polytone_inject: learned from your edits to the draft"
DRAFT_EDIT_WINDOW_S = 600
TYPED_PERSONA = "Default"  # "persona = null": the global pool, not the active persona
WISPR_INSERTED_REASON = "wispr_inserted: the message is Wispr Flow dictation output"
SOURCE_MODULES: dict[str, str] = {
    "gmail": "backend.mcp_servers.gmail_outbound",
    "wispr": "backend.mcp_servers.wispr",
    "watcher": "backend.mcp_servers.file_watcher",
}


def _module(name: str) -> ModuleType | None:
    modname = SOURCE_MODULES.get(name)
    if not modname:
        return None
    try:
        return importlib.import_module(modname)
    except ImportError:
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("source module %s failed to import: %s", modname, e)
        return None


def _call(mod: ModuleType | None, fn: str, *args: Any, default: Any = None) -> Any:
    f = getattr(mod, fn, None) if mod else None
    if not callable(f):
        return default
    try:
        return f(*args)
    except Exception as e:  # noqa: BLE001
        log.warning("%s.%s failed: %s", mod.__name__, fn, type(e).__name__)
        return default


def describe_source(name: str, rt: Runtime) -> dict[str, Any]:
    runs = rt.db.list_ingest_runs(source=name, limit=1)
    last = runs[0] if runs else None
    if name == "manual":
        enabled, status = True, "ready"
    elif name == "typed":
        enabled = rt.db.capture_config()[0]
        status = "listening" if enabled else "disabled"
    else:
        mod = _module(name)
        has_collect = callable(getattr(mod, "collect", None)) if mod else False
        enabled = bool(_call(mod, "is_enabled", rt.settings, default=has_collect)) and has_collect
        status = _call(mod, "status", rt.settings, default=None)
        if status is None:
            status = "unavailable" if not has_collect else ("ready" if enabled else "disabled")
    if last and last["status"] == "error":
        status = "error"
    return {
        "name": name,
        "enabled": enabled,
        "status": str(status),
        "last_run": (last["finished_at"] or last["started_at"]) if last else None,
        "items_ingested": rt.db.count_samples(source=name),
    }


def list_sources(rt: Runtime) -> list[dict[str, Any]]:
    return [describe_source(n, rt) for n in SOURCES]


def run_source(name: str, rt: Runtime, persona: str | None = None) -> dict[str, Any]:
    from backend.langgraph.ingest_graph import empty_result, run_ingest_batch

    if name not in SOURCES:
        raise KeyError(name)
    if name == "manual":
        res = empty_result()
        res["reasons"].append("manual source has nothing to collect; POST /ingest/manual instead")
        return res
    if name in PUSH_ONLY:
        res = empty_result()
        res["reasons"].append(f"{name} is push-only; the app POSTs /ingest/{name} as you send messages")
        return res
    mod = _module(name)
    collect = getattr(mod, "collect", None) if mod else None
    if not callable(collect):
        res = empty_result()
        res["reasons"].append(f"source '{name}' is not available yet")
        return res
    if not _call(mod, "is_enabled", rt.settings, default=True):
        res = empty_result()
        res["reasons"].append(f"source '{name}' is disabled or not configured")
        return res

    def items():
        yield from collect(rt.settings) or []

    return run_ingest_batch(items(), name, persona, runtime=rt)


def ingest_typed(
    rt: Runtime, text: str, app: str, app_name: str | None = None, after_inject: bool = False
) -> dict[str, Any]:
    """One message the user typed and sent in a native app (typing capture).

    ``after_inject`` → the message followed a Polytone inject in the same app, so it is (an edit
    of) a generated draft: it is stored in ``draft_edits`` and never ingested as a sample. When it
    matches a recent draft, the differences from that draft update the draft persona's
    corrections (``backend/learning/edits.py``); the text itself is not learned. Otherwise it
    goes through the full ingest graph as source ``typed`` under the Default persona (the global
    pool — a typed message is not tied to whichever persona happens to be active).
    """
    from backend.langgraph.ingest_graph import empty_result, run_ingest_batch

    text = (text or "")[:TYPED_MAX_CHARS]
    if after_inject:
        learned = _learn_from_edit(rt, text, app)
        rt.audit_event_soon("ingest", {"source": "typed", "accepted": 0, "rejected": 1, "draft_edit": True,
                                       "edit_learned": learned, "app": (app or "")[:100]})
        res = empty_result()
        res.update(rejected=1, reasons=[AFTER_INJECT_LEARNED if learned else AFTER_INJECT_REASON])
        return res
    if not rt.db.capture_config()[0]:
        res = empty_result()
        res.update(rejected=1, reasons=["typing capture is disabled (capture_enabled=false)"])
        return res
    prov: dict[str, Any] = {"typed": True, "app": app, "generated": False, "direction": "outbound"}
    if app_name:
        prov["app_name"] = app_name
    stripped = _strip_wispr(text, app)
    if stripped is not None and stripped.matched:
        from backend.ingestion import firewall

        ok, why = firewall.check(stripped.text, "typed", prov, rt.db)
        if not ok:
            res = empty_result()
            res.update(rejected=1, reasons=[f"{WISPR_INSERTED_REASON} ({why})"])
            return res
        prov["wispr_removed"] = {"rowids": stripped.matched, "tokens": stripped.removed_tokens}
        text = stripped.text
    return run_ingest_batch([{"text": text, "provenance": prov}], "typed", TYPED_PERSONA, runtime=rt)


def _learn_from_edit(rt: Runtime, text: str, app: str | None) -> bool:
    """Store the post-inject message, linked to the draft it edits, and learn from the diff.
    Returns True when a draft matched and its persona's corrections were updated."""
    from backend.learning import edits

    match = None
    try:
        match = edits.match_draft(rt.db, text, DRAFT_EDIT_WINDOW_S)
    except Exception as e:  # noqa: BLE001 - matching is a bonus; storing the edit is not
        log.warning("draft matching failed: %s", type(e).__name__)
    rt.db.insert_draft_edit(app=app or "", text=text, nearest_draft_id=match[0]["draft_id"] if match else None)
    if match is None:
        return False
    draft = match[0]
    try:
        from backend.learning.outcomes import edit_outcome

        rt.db.set_draft_outcome(draft["draft_id"], edit_outcome(draft["text"], text))
    except Exception as e:  # noqa: BLE001 - the outcome label is a bonus
        log.warning("draft outcome update failed: %s", type(e).__name__)
    try:
        edits.learn_edit(rt.db, draft["persona"], draft["text"], text)
    except Exception as e:  # noqa: BLE001 - never fail the capture request over learning
        log.warning("edit learning failed: %s", type(e).__name__)
        return False
    edits.invalidate(draft["persona"])
    return True


def _strip_wispr(text: str, app: str | None) -> Any:
    """Spans of a typed message that Wispr Flow inserted (its AI-formatted dictation output),
    or None when Wispr isn't installed / the check failed. See ``mcp_servers.wispr_live``."""
    try:
        from backend.mcp_servers import wispr_live

        return wispr_live.check_typed(text, app)
    except Exception as e:  # noqa: BLE001 - typing capture must work without Wispr
        log.warning("wispr insertion check failed: %s", type(e).__name__)
        return None
