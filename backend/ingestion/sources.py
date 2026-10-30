"""Ingestion sources registry used by ``/ingest/sources`` and ``/ingest/sources/{name}/run``.

Each non-manual source maps to a module in ``backend.mcp_servers``. A source module may expose
(all optional; a missing module or missing ``collect`` makes the source "unavailable"):

    def collect(settings) -> Iterable[dict | str]
        # yields human text items: {"text": str, "provenance": dict?, "persona": str?}
        # provenance should carry e.g. {"direction": "outbound"} / {"generated": False}
    def is_enabled(settings) -> bool          # default: True when collect exists
    def status(settings) -> str               # default: last run status or "ready"

Collected items still pass through the full ingest graph (validate → firewall → ...).
"""
from __future__ import annotations

import importlib
import logging
from types import ModuleType
from typing import Any

from backend.runtime import Runtime

log = logging.getLogger(__name__)

SOURCES: tuple[str, ...] = ("manual", "gmail", "wispr", "watcher")
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
