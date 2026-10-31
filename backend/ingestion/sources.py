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
is refused; it is "enabled" exactly when ``capture_enabled`` is set in ``/config``.
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
DRAFT_EDIT_WINDOW_S = 600
TYPED_PERSONA = "Default"  # "persona = null": the global pool, not the active persona
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
        log.warning("%s.%s failed: %s", mod.__name__, fn, e)
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
    of) a generated draft: it is stored in ``draft_edits`` and NEVER learned from. Otherwise it
    goes through the full ingest graph as source ``typed`` under the Default persona (the global
    pool — a typed message is not tied to whichever persona happens to be active).
    """
    from backend.langgraph.ingest_graph import empty_result, run_ingest_batch

    text = (text or "")[:TYPED_MAX_CHARS]
    if after_inject:
        rt.db.insert_draft_edit(
            app=app or "", text=text, nearest_draft_id=rt.db.recent_draft_id(DRAFT_EDIT_WINDOW_S)
        )
        res = empty_result()
        res.update(rejected=1, reasons=[AFTER_INJECT_REASON])
        return res
    if not rt.db.capture_config()[0]:
        res = empty_result()
        res.update(rejected=1, reasons=["typing capture is disabled (capture_enabled=false)"])
        return res
    prov: dict[str, Any] = {"typed": True, "app": app, "generated": False, "direction": "outbound"}
    if app_name:
        prov["app_name"] = app_name
    return run_ingest_batch([{"text": text, "provenance": prov}], "typed", TYPED_PERSONA, runtime=rt)
