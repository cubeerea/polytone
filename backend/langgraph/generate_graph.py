"""Generation LangGraph:

    capture → retrieve_constraints → build_prompt → generate → postprocess → record → score

``run_generation(intent, persona, app_context) -> dict`` returns the ``/generate`` response
(``draft_id, text, persona, latency_ms, constraints``). Generated text is recorded in SQLite
``drafts`` only — it is NEVER written to the graph.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from backend.graph.constraints import empty_constraints
from backend.langgraph.prompts import build_system_prompt, build_user_prompt
from backend.llm.client import MockLLM
from backend.runtime import Runtime, get_runtime

log = logging.getLogger(__name__)


class GenState(TypedDict, total=False):
    raw_intent: str
    intent: str
    persona: str
    app_context: str | None
    t0: float
    constraints: dict[str, Any]
    constraints_source: str  # "graph" | "fallback"
    system: str
    user: str
    raw_output: str
    llm_fallback: bool
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
        persona = (state.get("persona") or "").strip() or rt.db.get_config().get("active_persona") or "Default"
        return {"intent": intent, "persona": persona}

    def retrieve_constraints(state: GenState) -> GenState:
        try:
            c = rt.store.get_constraints(state["persona"])
            return {"constraints": dict(c), "constraints_source": "graph"}
        except Exception as e:  # noqa: BLE001 - graph trouble must never break generation
            log.warning("constraint retrieval failed (%s); using empty constraints", e)
            return {"constraints": dict(empty_constraints()), "constraints_source": "fallback"}

    def build_prompt(state: GenState) -> GenState:
        return {
            "system": build_system_prompt(state["constraints"], state["persona"], state.get("app_context")),
            "user": build_user_prompt(state["intent"]),
        }

    def generate(state: GenState) -> GenState:
        try:
            return {"raw_output": rt.llm.complete(state["system"], state["user"]), "llm_fallback": False}
        except Exception as e:  # noqa: BLE001 - degrade to the deterministic mock
            log.warning("LLM call failed (%s); falling back to MockLLM", e)
            return {"raw_output": MockLLM().complete(state["system"], state["user"]), "llm_fallback": True}

    def postprocess(state: GenState) -> GenState:
        text = postprocess_text(state.get("raw_output", ""))
        if not text:
            text = MockLLM().complete(state["system"], state["user"])
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
        baseline = [s["text"] for s in rt.db.list_samples(persona=persona, limit=200)]
        if not baseline:
            baseline = [s["text"] for s in rt.db.list_samples(limit=200)]
        if not baseline:
            return None
        result = scorer(text, baseline)
        if result is not None and not isinstance(result, dict):
            result = dict(result)
        rt.db.update_draft_score(draft_id, result)
        return result
    except Exception as e:  # noqa: BLE001 - scoring is best-effort
        log.warning("scoring draft %s failed: %s", draft_id, e)
        return None


def _graph_for(rt: Runtime):
    g = rt._graphs.get("generate")
    if g is None:
        g = rt._graphs["generate"] = build_generate_graph(rt)
    return g


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
    }
