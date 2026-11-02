"""Generation LangGraph:

    capture → retrieve_constraints → build_prompt → generate → postprocess → record → score

``run_generation(intent, persona, app_context) -> dict`` returns the ``/generate`` response
(``draft_id, text, persona, latency_ms, constraints``). Generated text is recorded in SQLite
``drafts`` only — it is NEVER written to the graph.

``retrieve_constraints`` also picks 2–3 few-shot excerpts of the user's real samples
(``langgraph/examples.py``) for the prompt.

LLM failures are never papered over: the mock LLM is used only when
``settings.llm_backend == "mock"``. A failing / empty real completion raises
``LLMFailedError`` (HTTP 502 ``{"error":"llm_failed"}``) or ``LLMTimeoutError`` (HTTP 504
``{"error":"llm_timeout"}``) and no draft is recorded.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from backend import personas
from backend.graph.constraints import empty_constraints
from backend.langgraph.examples import select_examples
from backend.langgraph.prompts import build_system_parts, build_user_prompt
from backend.learning.edits import corrected
from backend.llm.client import FAST_MODEL
from backend.runtime import Runtime, get_runtime

log = logging.getLogger(__name__)


class GenerationError(RuntimeError):
    """The draft could not be produced; maps to an HTTP error body ``{"error", "detail"}``."""

    status_code = 502
    code = "llm_failed"


class LLMFailedError(GenerationError):
    pass


class LLMTimeoutError(GenerationError):
    status_code = 504
    code = "llm_timeout"


def _is_timeout(e: BaseException | None) -> bool:
    seen = 0
    while e is not None and seen < 5:
        if isinstance(e, TimeoutError) or "timeout" in type(e).__name__.lower():
            return True
        e = e.__cause__ or e.__context__
        seen += 1
    return False


def _detail(e: BaseException) -> str:
    msg = str(e).strip()
    return f"{type(e).__name__}: {msg}"[:300] if msg else type(e).__name__


class GenState(TypedDict, total=False):
    raw_intent: str
    intent: str
    persona: str
    guidance: str | None  # the persona's tone line (backend.personas.guidance)
    model: str | None  # per-draft model override (draft_speed "fast"); None = the configured model
    app_context: str | None
    t0: float
    constraints: dict[str, Any]
    constraints_source: str  # "graph" | "fallback"
    examples: list[str]
    system: str
    system_parts: tuple[str, str]  # (stable, volatile) — the stable part is the prompt-cache prefix
    user: str
    raw_output: str
    text: str
    draft_id: str
    latency_ms: int
    score: dict[str, Any] | None


_BULLET_RE = re.compile(r"^\s*(?:[-*•·◦‣>]+|\d+[.)]|\[[ xX]?\])\s+")


def normalize_intent(raw: str) -> str:
    """Strip bullets / numbering / checkboxes and blank lines; keep one item per line."""
    lines = []
    for ln in (raw or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        ln = _BULLET_RE.sub("", ln).strip()
        ln = re.sub(r"\s+", " ", ln)
        if ln:
            lines.append(ln)
    return "\n".join(lines)


_PREAMBLE_RE = re.compile(
    r"^\s*(?:(?:sure|okay|ok|certainly|of course|absolutely)[,!.]?\s*)?"
    r"(?:here(?:'s| is| are)\b[^\n:]*:|(?:draft|message|rewrite|rewritten(?: message)?|final(?: text)?)\s*:)\s*",
    re.IGNORECASE,
)
_LEADING_ACK_RE = re.compile(r"^\s*(?:sure|okay|certainly|of course)[,!.]\s*\n+", re.IGNORECASE)
_TRAILING_RE = re.compile(r"\n+\s*(?:let me know if[^\n]*|i hope this helps[^\n]*|feel free to[^\n]*)\s*$", re.IGNORECASE)
_QUOTE_PAIRS = [('"', '"'), ("“", "”"), ("'", "'"), ("‘", "’"), ("`", "`")]


def postprocess_text(raw: str) -> str:
    t = (raw or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\n?|\n?```$", "", t).strip()
    for _ in range(2):
        t = _LEADING_ACK_RE.sub("", t)
        t = _PREAMBLE_RE.sub("", t, count=1).strip()
    t = _TRAILING_RE.sub("", t).strip()
    for a, b in _QUOTE_PAIRS:
        if len(t) >= 2 and t.startswith(a) and t.endswith(b):
            inner = t[len(a) : -len(b)]
            if a not in inner and b not in inner:
                t = inner.strip()
            break
    return t


def build_generate_graph(rt: Runtime):
    def capture(state: GenState) -> GenState:
        intent = normalize_intent(state.get("raw_intent", ""))
        if not intent:
            raise ValueError("intent is empty")
        cfg = rt.db.get_config()
        persona = (state.get("persona") or "").strip() or cfg.get("active_persona") or "Default"
        persona = personas.canonical(rt.db, persona) or "Default"
        model = FAST_MODEL if cfg.get("draft_speed") == "fast" else None
        return {"intent": intent, "persona": persona, "model": model, "guidance": personas.guidance(rt.db, persona)}

    def retrieve_constraints(state: GenState) -> GenState:
        try:
            c = rt.store.get_constraints(state["persona"])
            out: GenState = {"constraints": dict(c), "constraints_source": "graph"}
            try:
                out["constraints"] = corrected(rt.db, state["persona"], out["constraints"])
            except Exception as e:  # noqa: BLE001 - edit learning is an optional layer
                log.warning("edit corrections failed: %s", type(e).__name__)
        except Exception as e:  # noqa: BLE001 - graph trouble must never break generation
            log.warning("constraint retrieval failed (%s); using empty constraints", type(e).__name__)
            out = {"constraints": dict(empty_constraints()), "constraints_source": "fallback"}
        try:
            out["examples"] = select_examples(rt.db, state["persona"], state["intent"], out["constraints"])
        except Exception as e:  # noqa: BLE001 - examples are an optional boost
            log.warning("few-shot example selection failed: %s", type(e).__name__)
            out["examples"] = []
        return out

    def build_prompt(state: GenState) -> GenState:
        parts = build_system_parts(
            state["constraints"], state["persona"], state.get("app_context"), state.get("examples") or [],
            guidance=state.get("guidance"),
        )
        return {
            "system": "\n\n".join(p for p in parts if p),
            "system_parts": parts,
            "user": build_user_prompt(state["intent"]),
        }

    def generate(state: GenState) -> GenState:
        if getattr(rt.llm, "name", "") == "mock" and rt.settings.llm_backend != "mock":
            raise LLMFailedError("no LLM configured: POLYTONE_LLM=anthropic but ANTHROPIC_API_KEY is empty")
        try:
            system = state["system_parts"] if getattr(rt.llm, "cache_system", False) else state["system"]
            model = state.get("model")  # only passed when set: plain complete(system, user) LLMs keep working
            out = rt.llm.complete(system, state["user"], model=model) if model else rt.llm.complete(system, state["user"])
            return {"raw_output": out}
        except Exception as e:
            log.warning("LLM call failed: %s", _detail(e))
            if _is_timeout(e):
                raise LLMTimeoutError(_detail(e)) from e
            raise LLMFailedError(_detail(e)) from e

    def postprocess(state: GenState) -> GenState:
        text = postprocess_text(state.get("raw_output", ""))
        if not text:
            raise LLMFailedError("the model returned an empty completion")
        return {"text": text}

    def record(state: GenState) -> GenState:
        latency = round((time.perf_counter() - state["t0"]) * 1000)
        row = rt.db.insert_draft(
            persona=state["persona"],
            intent=state["intent"],
            text=state["text"],
            latency_ms=latency,
            constraints=state["constraints"],
        )
        return {"draft_id": row["draft_id"], "latency_ms": latency}

    def score(state: GenState) -> GenState:
        scorer = rt.get_scorer()
        if scorer is None:
            return {"score": None}
        if rt.score_async:
            rt.submit(score_draft, rt, state["draft_id"], state["text"], state["persona"])
            return {"score": None}
        return {"score": score_draft(rt, state["draft_id"], state["text"], state["persona"])}

    g = StateGraph(GenState)
    for name, fn in [
        ("capture", capture),
        ("retrieve_constraints", retrieve_constraints),
        ("build_prompt", build_prompt),
        ("generate", generate),
        ("postprocess", postprocess),
        ("record", record),
        ("score", score),
    ]:
        g.add_node(name, fn)
    g.add_edge(START, "capture")
    g.add_edge("capture", "retrieve_constraints")
    g.add_edge("retrieve_constraints", "build_prompt")
    g.add_edge("build_prompt", "generate")
    g.add_edge("generate", "postprocess")
    g.add_edge("postprocess", "record")
    g.add_edge("record", "score")
    g.add_edge("score", END)
    return g.compile()


def score_draft(rt: Runtime, draft_id: str, text: str, persona: str) -> dict | None:
    """Score a draft against the persona's human samples and persist the breakdown."""
    scorer = rt.get_scorer()
    if scorer is None:
        return None
    try:
        from backend.evaluation.style_distance import baseline_texts

        baseline = baseline_texts(rt.db, persona)
        if not baseline:
            return None
        result = scorer(text, baseline, persona)
        if result is not None and not isinstance(result, dict):
            result = dict(result)
        rt.db.update_draft_score(draft_id, result)
        return result
    except Exception as e:  # noqa: BLE001 - scoring is best-effort
        log.warning("scoring draft %s failed: %s", draft_id, type(e).__name__)
        return None


def _graph_for(rt: Runtime):
    g = rt._graphs.get("generate")
    if g is None:
        g = rt._graphs["generate"] = build_generate_graph(rt)
    return g


def warm(rt: Runtime) -> None:
    """Startup / after ingest: compile the graph, prime the active persona's constraints and its
    few-shot excerpt cache (~40ms the first draft would otherwise pay), and open the API connection."""
    _graph_for(rt)
    rt.prime_constraints()
    try:
        persona = rt.db.get_config().get("active_persona") or "Default"
        c = rt.store.get_constraints(persona)
        select_examples(rt.db, persona, "warm up the example cache", dict(c))
    except Exception as e:  # noqa: BLE001 - only a warm-up
        log.debug("example cache warm-up failed: %s", type(e).__name__)
    warm_llm = getattr(rt.llm, "warm", None)
    if warm_llm is not None:
        warm_llm()


def run_generation(
    intent: str,
    persona: str | None = None,
    app_context: str | None = None,
    *,
    runtime: Runtime | None = None,
) -> dict[str, Any]:
    rt = runtime or get_runtime()
    out = _graph_for(rt).invoke(
        {"raw_intent": intent, "persona": persona or "", "app_context": app_context, "t0": time.perf_counter()}
    )
    return {
        "draft_id": out["draft_id"],
        "text": out["text"],
        "persona": out["persona"],
        "latency_ms": out["latency_ms"],
        "constraints": out["constraints"],
        "model": out.get("model") or getattr(rt.llm, "model", None) or rt.llm.name,  # audit only
    }
