"""Ingestion LangGraph:  validate → firewall → extract → write_graph → record

``validate`` strips quoted reply chains / signatures (``text.clean_message``); ``firewall``
enforces provenance, minimum length, sample de-duplication and the never-a-draft rules.

``run_ingest(text, source, persona, provenance) -> IngestResult`` ingests ONE human sample.
``run_ingest_batch(items, source, persona)`` ingests many and logs an ``ingest_runs`` row.

IngestResult = {"accepted", "rejected", "reasons", "sentences", "words_merged"}.
Only text that passes the firewall reaches the graph; drafts never do.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from backend.ingestion import extract as extract_mod
from backend.ingestion import firewall as firewall_mod
from backend.ingestion.text import clean_message
from backend.ingestion.types import Extraction
from backend.runtime import Runtime, get_runtime
from backend.storage.db import utcnow_iso

log = logging.getLogger(__name__)

MAX_SAMPLE_CHARS = 200_000


class IngestState(TypedDict, total=False):
    text: str
    source: str
    persona: str
    provenance: dict[str, Any]
    ok: bool
    reason: str | None
    extraction: Extraction
    sentences: int
    words_merged: int
    sample_id: int | None


def empty_result() -> dict[str, Any]:
    return {"accepted": 0, "rejected": 0, "reasons": [], "sentences": 0, "words_merged": 0}


def build_ingest_graph(rt: Runtime):
    def validate(state: IngestState) -> IngestState:
        raw = (state.get("text") or "").strip()
        if not raw:
            return {"ok": False, "reason": "empty text"}
        if len(raw) > MAX_SAMPLE_CHARS:
            return {"ok": False, "reason": f"sample too long (> {MAX_SAMPLE_CHARS} chars)"}
        # keep only the user's own words: drop quoted reply chains and signatures; the cleaned
        # text is what the firewall checks, the graph learns and SQLite stores
        text = clean_message(raw)
        if not text:
            return {"ok": False, "reason": "nothing written by the user (only quoted reply / signature)"}
        return {"ok": True, "text": text}

    def firewall(state: IngestState) -> IngestState:
        ok, reason = firewall_mod.check(state["text"], state["source"], state.get("provenance") or {}, rt.db)
        return {"ok": bool(ok), "reason": reason}

    def extract(state: IngestState) -> IngestState:
        ex = extract_mod.extract(state["text"])
        if not ex.sentence_lengths:
            return {"ok": False, "reason": "no sentences found"}
        return {"extraction": ex}

    def write_graph(state: IngestState) -> IngestState:
        try:
            res = rt.store.write_extraction(state["extraction"], state["persona"])
        except Exception as e:  # noqa: BLE001
            log.warning("graph write failed: %s", e)
            return {"ok": False, "reason": f"graph write failed: {e.__class__.__name__}"}
        return {"sentences": int(res.get("sentences", 0)), "words_merged": int(res.get("words_merged", 0))}

    def record(state: IngestState) -> IngestState:
        sid = rt.db.insert_sample(
            text=state["text"], source=state["source"], persona=state["persona"],
            provenance=state.get("provenance") or {},
        )
        return {"sample_id": sid}

    def gate(state: IngestState) -> str:
        return "go" if state.get("ok", True) else "stop"

    g = StateGraph(IngestState)
    g.add_node("validate", validate)
    g.add_node("firewall", firewall)
    g.add_node("extract", extract)
    g.add_node("write_graph", write_graph)
    g.add_node("record", record)
    g.add_edge(START, "validate")
    g.add_conditional_edges("validate", gate, {"go": "firewall", "stop": END})
    g.add_conditional_edges("firewall", gate, {"go": "extract", "stop": END})
    g.add_conditional_edges("extract", gate, {"go": "write_graph", "stop": END})
    g.add_conditional_edges("write_graph", gate, {"go": "record", "stop": END})
    g.add_edge("record", END)
    return g.compile()


def _graph_for(rt: Runtime):
    g = rt._graphs.get("ingest")
    if g is None:
        g = rt._graphs["ingest"] = build_ingest_graph(rt)
    return g


def run_ingest(
    text: str,
    source: str = "manual",
    persona: str | None = None,
    provenance: dict[str, Any] | None = None,
    *,
    runtime: Runtime | None = None,
) -> dict[str, Any]:
    rt = runtime or get_runtime()
    persona = (persona or "").strip() or rt.db.get_config().get("active_persona") or "Default"
    out = _graph_for(rt).invoke(
        {"text": text or "", "source": source, "persona": persona, "provenance": dict(provenance or {})}
    )
    res = empty_result()
    if out.get("ok", False):
        res.update(accepted=1, sentences=out.get("sentences", 0), words_merged=out.get("words_merged", 0))
    else:
        res.update(rejected=1, reasons=[out.get("reason") or "rejected"])
    return res


def run_ingest_batch(
    items: Iterable[dict[str, Any] | str],
    source: str,
    persona: str | None = None,
    *,
    runtime: Runtime | None = None,
) -> dict[str, Any]:
    """Ingest many items (``str`` or ``{"text", "provenance"?, "persona"?}``); logs an ingest run."""
    rt = runtime or get_runtime()
    started = utcnow_iso()
    total = empty_result()
    status = "ok"
    try:
        for item in items:
            if isinstance(item, str):
                item = {"text": item}
            r = run_ingest(
                item.get("text", ""), source, item.get("persona") or persona, item.get("provenance"), runtime=rt
            )
            for k in ("accepted", "rejected", "sentences", "words_merged"):
                total[k] += r[k]
            for reason in r["reasons"]:
                if reason not in total["reasons"]:
                    total["reasons"].append(reason)
    except Exception as e:
        log.exception("ingest run for %s failed", source)
        status = "error"
        total["reasons"].append(f"source error: {e}")
    finally:
        rt.db.record_ingest_run(
            source=source, started_at=started, finished_at=utcnow_iso(),
            accepted=total["accepted"], rejected=total["rejected"], status=status,
        )
    return total
