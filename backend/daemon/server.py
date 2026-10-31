"""FastAPI daemon implementing the HTTP API in CONTRACTS.md.

``create_app(runtime=None)`` builds the app (tests pass an explicit Runtime);
``main()`` runs uvicorn on 127.0.0.1:settings.polytone_api_port.
"""
from __future__ import annotations

import logging
import os
import threading
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from backend.config import get_settings
from backend.ingestion.sources import PUSH_ONLY, SOURCES, ingest_typed, list_sources, run_source
from backend.langgraph.generate_graph import GenerationError, run_generation
from backend.langgraph.ingest_graph import run_ingest_batch
from backend.langgraph.prompts import PERSONAS
from backend.runtime import Runtime, build_runtime, get_runtime, set_runtime

log = logging.getLogger("polytone.api")

CORS_ORIGINS = ["tauri://localhost", "http://tauri.localhost", "https://tauri.localhost", "http://localhost:1420"]


class GenerateRequest(BaseModel):
    intent: str
    persona: str | None = None
    app_context: str | None = None


class IngestManualRequest(BaseModel):
    text: str
    persona: str | None = None


class IngestTypedRequest(BaseModel):
    text: str
    app: str
    app_name: str | None = None
    after_inject: bool = False


class ConfigBody(BaseModel):
    active_persona: str | None = Field(default=None, min_length=1)
    hotkey: str | None = Field(default=None, min_length=1)
    inject_mode: Literal["paste", "type"] | None = None
    capture_enabled: bool | None = None
    capture_apps: list[str] | None = None

    @field_validator("capture_apps")
    @classmethod
    def _clean_apps(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        out: list[str] = []
        for a in v:
            a = a.strip()
            if a and a not in out:
                out.append(a)
        return out


def _config_out(db: Any) -> dict[str, Any]:
    cfg = db.get_config()
    enabled, apps = db.capture_config()
    return {
        "active_persona": cfg.get("active_persona", "Default"),
        "hotkey": cfg.get("hotkey", "CmdOrCtrl+Shift+P"),
        "inject_mode": cfg.get("inject_mode", "paste"),
        "capture_enabled": enabled,
        "capture_apps": apps,
    }


def _start_watcher(r: Runtime) -> Any:
    """Start the drop-folder watcher thread if POLYTONE_WATCHER (default 1) and the dir exists."""
    if os.environ.get("POLYTONE_WATCHER", "1").strip().lower() in ("0", "false", "no", "off"):
        return None
    if not r.settings.polytone_watch_dir.is_dir():
        return None
    try:
        from backend.mcp_servers.file_watcher import start_watcher

        t = start_watcher(r)
        log.info("watching %s for new writing samples", r.settings.polytone_watch_dir)
        return t
    except Exception as e:  # noqa: BLE001 - the API must start even if the watcher can't
        log.warning("file watcher not started: %s", e)
        return None


def _warm_embeddings(r: Runtime) -> threading.Thread | None:
    """Load the Qwen embedding model in a daemon thread so the first draft's score isn't delayed.

    Only when POLYTONE_EMBEDDINGS=qwen; never blocks startup and never raises.
    """
    if (r.settings.polytone_embeddings or "").strip().lower() != "qwen":
        return None

    def _load() -> None:
        try:
            from backend.evaluation import embeddings

            if embeddings.get_model() is not None:
                log.info("embedding model warm (%.1fs)", embeddings.load_seconds() or 0.0)
        except Exception as e:  # noqa: BLE001 - optional component
            log.warning("embedding warm-up failed: %s", e)

    t = threading.Thread(target=_load, name="polytone-embed-warmup", daemon=True)
    t.start()
    return t


def create_app(runtime: Runtime | None = None) -> FastAPI:
    holder: dict[str, Runtime] = {}

    def rt() -> Runtime:
        if "rt" not in holder:
            holder["rt"] = runtime or get_runtime()
        return holder["rt"]

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        r = rt()
        log.info("graph store: %s, llm: %s", r.store.kind, r.llm.name)
        if r.store.kind != "neo4j":
            log.warning("Neo4j not in use — style graph is in-memory and will not persist")
        watcher = _start_watcher(r)
        _warm_embeddings(r)
        yield
        if watcher is not None:
            from backend.mcp_servers.file_watcher import stop_watcher

            stop_watcher(watcher)
        if runtime is None:
            r.close()

    app = FastAPI(title="Polytone", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def health() -> dict[str, Any]:
        r = rt()
        neo4j_ok = r.store.kind == "neo4j" and r.store.healthy()
        configured = "anthropic" if r.settings.llm_backend == "anthropic" else "mock"
        return {
            "status": "ok",
            "neo4j": bool(neo4j_ok),
            # the configured backend; `llm_ready` is False when it can't actually run
            # (POLYTONE_LLM=anthropic without a key → /generate answers 502)
            "llm": configured,
            "llm_ready": configured == "mock" or r.llm.name == "anthropic",
            "embeddings": "none" if r.settings.polytone_embeddings == "none" else "qwen",
            "graph_store": r.store.kind,
        }

    @app.post("/generate", response_model=None)
    def generate(req: GenerateRequest) -> dict[str, Any] | JSONResponse:
        if not req.intent or not req.intent.strip():
            raise HTTPException(status_code=422, detail="intent is empty")
        try:
            return run_generation(req.intent, req.persona, req.app_context, runtime=rt())
        except GenerationError as e:  # never inject placeholder text: tell the client it failed
            return JSONResponse(status_code=e.status_code, content={"error": e.code, "detail": str(e)})
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e

    @app.post("/ingest/manual")
    def ingest_manual(req: IngestManualRequest) -> dict[str, Any]:
        item = {"text": req.text, "provenance": {"source": "manual", "generated": False, "direction": "outbound"}}
        return run_ingest_batch([item], "manual", req.persona, runtime=rt())

    @app.post("/ingest/typed")
    def ingest_typed_route(req: IngestTypedRequest) -> dict[str, Any]:
        return ingest_typed(rt(), req.text, req.app, req.app_name, req.after_inject)

    @app.get("/ingest/sources")
    def ingest_sources() -> list[dict[str, Any]]:
        return list_sources(rt())

    @app.post("/ingest/sources/{name}/run")
    def ingest_source_run(name: str) -> dict[str, Any]:
        if name not in SOURCES:
            raise HTTPException(status_code=404, detail=f"unknown source '{name}'")
        if name in PUSH_ONLY:
            raise HTTPException(status_code=400, detail=f"{name} is push-only")
        return run_source(name, rt())

    @app.get("/personas")
    def personas() -> list[dict[str, str]]:
        out = [{"name": n, "description": d} for n, d in PERSONAS.items()]
        known = set(PERSONAS)
        for n in rt().db.sample_personas():
            if n not in known:
                out.append({"name": n, "description": f"Custom persona learned from your '{n}' samples."})
                known.add(n)
        return out

    @app.get("/config")
    def get_config() -> dict[str, Any]:
        return _config_out(rt().db)

    @app.put("/config")
    def put_config(body: ConfigBody) -> dict[str, Any]:
        rt().db.set_config(body.model_dump(exclude_none=True))
        return _config_out(rt().db)

    @app.get("/drafts")
    def drafts(limit: int = Query(50, ge=1, le=1000)) -> list[dict[str, Any]]:
        return [
            {
                "draft_id": d["draft_id"],
                "created_at": d["created_at"],
                "persona": d["persona"],
                "intent": d["intent"],
                "text": d["text"],
                "latency_ms": d["latency_ms"],
                "score": d["score"],
            }
            for d in rt().db.list_drafts(limit)
        ]

    @app.get("/scores/summary")
    def scores_summary() -> dict[str, Any]:
        trend = []
        for t, s in rt().db.scored_drafts():
            try:
                trend.append({"t": t, "score": float(s["score"])})
            except (KeyError, TypeError, ValueError):
                continue
        mean = round(sum(x["score"] for x in trend) / len(trend), 2) if trend else None
        return {"mean": mean, "count": len(trend), "trend": trend[-200:]}

    @app.get("/graph")
    def graph(limit: int = Query(60, ge=1, le=1000), persona: str | None = None) -> dict[str, Any]:
        try:
            return rt().store.get_graph(limit=limit, persona=persona or None)
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"graph unavailable: {e.__class__.__name__}") from e

    @app.get("/stats")
    def stats() -> dict[str, Any]:
        r = rt()
        try:
            g = r.store.stats()
        except Exception as e:  # noqa: BLE001
            log.warning("graph stats failed: %s", e)
            g = {"words": 0, "structures": 0, "samples": 0, "avg_sentence_len": None}
        return {
            "words": int(g["words"]),
            "structures": int(g["structures"]),
            "samples": r.db.count_samples(),
            "avg_sentence_len": g["avg_sentence_len"],
        }

    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # "constraint already exists" etc. are INFO-level server notifications — just noise here
    logging.getLogger("neo4j.notifications").setLevel(logging.WARNING)
    from backend.daemon.watchdog import start_parent_watchdog

    start_parent_watchdog()
    settings = get_settings()
    rt = build_runtime(settings, score_async=True)
    set_runtime(rt)
    uvicorn.run(create_app(rt), host="127.0.0.1", port=settings.polytone_api_port, log_level="info")
    rt.close()


if __name__ == "__main__":
    main()
