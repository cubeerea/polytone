"""FastAPI daemon implementing the HTTP API in CONTRACTS.md.

``create_app(runtime=None)`` builds the app (tests pass an explicit Runtime);
``main()`` runs uvicorn on 127.0.0.1:settings.polytone_api_port.

Startup: ``/health`` answers as soon as uvicorn is up. LangGraph (~0.3s of imports) is imported
lazily by the routes that need it; one background thread (``_warm_up``) then compiles the
generate graph, primes the active persona's constraints, starts the drop-folder watcher and the
live Wispr Flow watcher (``mcp_servers/wispr_live.py``) and finally loads the embedding model, so the first hotkey press pays for none of it.

Security (CONTRACTS.md → Security): every route except ``GET /health`` needs a bearer token
(``security/auth.py``: the app token Rust holds, or a UI session from ``POST /session``), the Host
header must be loopback, and security-relevant actions are written to the audit log
(``security/audit.py``). A retention task purges old typed/wispr samples and drafts daily.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from backend import personas as persona_store
from backend.config import REPO_ROOT, get_settings
from backend.ingestion.sources import (
    PUSH_ONLY,
    SOURCES,
    ingest_typed,
    list_sources,
    run_source,
)
from backend.runtime import Runtime, build_runtime, get_runtime, set_runtime
from backend.security import files as file_hygiene
from backend.security import retention
from backend.security.auth import (
    MAX_IDLE_MINUTES,
    MIN_IDLE_MINUTES,
    AuthManager,
    AuthMiddleware,
)

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


class PersonaCreateBody(BaseModel):
    name: str
    description: str


class PersonaUpdateBody(BaseModel):
    description: str | None = None  # None on a built-in: reset to Polytone's wording


class ConfigBody(BaseModel):
    active_persona: str | None = Field(default=None, min_length=1)
    hotkey: str | None = Field(default=None, min_length=1)
    inject_mode: Literal["paste", "type"] | None = None
    capture_enabled: bool | None = None
    capture_apps: list[str] | None = None
    draft_speed: Literal["quality", "fast"] | None = None
    lock_idle_minutes: int | None = Field(default=None, ge=MIN_IDLE_MINUTES, le=MAX_IDLE_MINUTES)
    retention_days: int | None = Field(default=None, ge=0, le=retention.MAX_RETENTION_DAYS)

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


class AuditEventBody(BaseModel):
    event: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$", max_length=64)
    detail: dict[str, Any] = Field(default_factory=dict)


class WipeBody(BaseModel):
    confirm: str


WIPE_PHRASE = "delete everything"


def _int_cfg(cfg: dict[str, str], key: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(cfg.get(key, default))))
    except (TypeError, ValueError):
        return default


def _config_out(db: Any) -> dict[str, Any]:
    cfg = db.get_config()
    enabled, apps = db.capture_config()
    return {
        "active_persona": cfg.get("active_persona", "Default"),
        "hotkey": cfg.get("hotkey", "CmdOrCtrl+Shift+P"),
        "inject_mode": cfg.get("inject_mode", "paste"),
        "draft_speed": "fast" if cfg.get("draft_speed") == "fast" else "quality",
        "capture_enabled": enabled,
        "capture_apps": apps,
        "lock_idle_minutes": _int_cfg(cfg, "lock_idle_minutes", 15, MIN_IDLE_MINUTES, MAX_IDLE_MINUTES),
        "retention_days": _int_cfg(cfg, "retention_days", 0, 0, retention.MAX_RETENTION_DAYS),
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


def _start_wispr_live(r: Runtime) -> Any:
    """Start the live Wispr Flow watcher if POLYTONE_WISPR_LIVE (default 1) and Wispr's DB exists."""
    try:
        from backend.mcp_servers.wispr_live import start_live

        return start_live(r)
    except Exception as e:  # noqa: BLE001 - the API must start even if Wispr can't be watched
        log.warning("wispr live watcher not started: %s", e)
        return None


def _warm_embeddings(r: Runtime) -> None:
    """Load the Qwen embedding model (+ one tiny encode) when POLYTONE_EMBEDDINGS=qwen. Never raises."""
    if (r.settings.polytone_embeddings or "").strip().lower() != "qwen":
        return
    try:
        from backend.evaluation import embeddings

        if embeddings.warm_up():
            log.info("embedding model warm (load %.1fs)", embeddings.load_seconds() or 0.0)
    except Exception as e:  # noqa: BLE001 - optional component
        log.warning("embedding warm-up failed: %s", e)


def _warm_up(r: Runtime, watcher: dict[str, Any], stopping: threading.Event) -> None:
    """Background start-up work, cheapest-and-most-needed first (see module docstring)."""
    try:
        from backend.langgraph.generate_graph import warm

        warm(r)
    except Exception as e:  # noqa: BLE001
        log.warning("generation warm-up failed: %s", e)
    if not stopping.is_set():
        watcher["t"] = _start_watcher(r)
    if not stopping.is_set():
        watcher["wispr"] = _start_wispr_live(r)
    if not stopping.is_set():
        _warm_embeddings(r)


def _wispr_live() -> bool:
    mod = sys.modules.get("backend.mcp_servers.wispr_live")  # never imported → not running
    # the warmup thread may be mid-import (module in sys.modules, `active` not defined yet)
    active = getattr(mod, "active", None)
    return bool(active and active() is not None)


def create_app(runtime: Runtime | None = None, *, api_token: str | None = None) -> FastAPI:
    holder: dict[str, Runtime] = {}

    def rt() -> Runtime:
        if "rt" not in holder:
            holder["rt"] = runtime or get_runtime()
        return holder["rt"]

    r0 = rt()
    auth = AuthManager(
        r0.settings.polytone_data_dir,
        r0.settings.polytone_api_port,
        token=api_token,
        idle_minutes=_config_out(r0.db)["lock_idle_minutes"],
        audit=lambda event, detail, actor: rt().audit_event(event, detail, actor),
    )

    def actor(request: Request) -> str:
        return str(getattr(request.state, "actor", "unknown"))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        r = rt()
        log.info("graph store: %s, llm: %s", r.store.kind, r.llm.name)
        if r.store.kind != "neo4j":
            log.warning("Neo4j not in use — style graph is in-memory and will not persist")
        watcher: dict[str, Any] = {}
        stopping = threading.Event()
        warm = threading.Thread(target=_warm_up, args=(r, watcher, stopping), name="polytone-warmup", daemon=True)
        warm.start()
        app.state.warmup = warm  # tests join it
        file_hygiene.harden(r.settings.polytone_data_dir)
        purger = retention.RetentionTask(r).start()  # purge now, then daily
        yield
        purger.stop()
        stopping.set()
        warm.join(timeout=5)  # the watcher may still be starting; never wait on the embedding load
        if watcher.get("t") is not None:
            from backend.mcp_servers.file_watcher import stop_watcher

            stop_watcher(watcher["t"])
        if watcher.get("wispr") is not None:
            from backend.mcp_servers.wispr_live import stop_live

            stop_live(watcher["wispr"])
        if runtime is None:
            r.close()

    app = FastAPI(title="Polytone", version="0.1.0", lifespan=lifespan)
    app.state.auth = auth
    # order matters: CORS is outermost (added last) so preflights and 401s carry CORS headers
    app.add_middleware(AuthMiddleware, auth=auth)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def health(challenge: str | None = Query(None, min_length=16, max_length=128)) -> dict[str, Any]:
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
            "wispr_live": _wispr_live(),  # additive: the live Wispr Flow watcher is running
            # identity proof for Rust (CONTRACTS.md → Security); only with ?challenge=
            **({"proof": auth.health_proof(challenge)} if challenge else {}),
        }

    # ------------------------------------------------------------------ sessions / audit
    @app.post("/session")
    def create_session() -> dict[str, Any]:
        return auth.create_session()

    @app.delete("/session")
    def revoke_sessions() -> dict[str, Any]:
        return {"revoked": auth.revoke_all("app")}

    @app.delete("/session/current")
    def lock_current(request: Request) -> dict[str, Any]:
        if actor(request) != "ui":
            raise HTTPException(status_code=400, detail="not a session token")
        return {"revoked": int(auth.revoke(str(request.state.token)))}

    @app.get("/audit")
    def audit_entries(limit: int = Query(100, ge=1, le=1000), before_seq: int | None = Query(None, ge=1)):
        log_ = rt().audit
        if log_ is None:
            return {"entries": [], "head_seq": 0}
        return {"entries": log_.entries(limit, before_seq), "head_seq": log_.head_seq()}

    @app.get("/audit/verify")
    def audit_verify() -> dict[str, Any]:
        log_ = rt().audit
        if log_ is None:
            return {"ok": True, "count": 0, "first_bad_seq": None, "reason": None}
        return log_.verify()

    @app.post("/audit/event")
    def audit_event(body: AuditEventBody) -> dict[str, Any]:
        log_ = rt().audit
        seq = log_.append(body.event, body.detail, actor="app") if log_ is not None else -1
        return {"seq": seq}

    @app.post("/data/wipe")
    def data_wipe(body: WipeBody, request: Request) -> dict[str, Any]:
        if body.confirm != WIPE_PHRASE:
            raise HTTPException(status_code=400, detail=f'confirm must be "{WIPE_PHRASE}"')
        return {"deleted": retention.wipe(rt(), actor(request))}

    @app.post("/generate", response_model=None)
    def generate(req: GenerateRequest, request: Request) -> dict[str, Any] | JSONResponse:
        from backend.langgraph.generate_graph import GenerationError, run_generation

        if not req.intent or not req.intent.strip():
            raise HTTPException(status_code=422, detail="intent is empty")
        r = rt()
        try:
            out = run_generation(req.intent, req.persona, req.app_context, runtime=r)
            model = out.pop("model", None)  # the model this draft used: audit log only, not in the response
        except GenerationError as e:  # never inject placeholder text: tell the client it failed
            r.audit_event_soon("generate", {"ok": False, "error": e.code, "chars_in": len(req.intent)}, actor(request))
            return JSONResponse(status_code=e.status_code, content={"error": e.code, "detail": str(e)})
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        r.audit_event_soon(
            "generate",
            {"ok": True, "draft_id": out.get("draft_id"), "persona": out.get("persona"), "chars_in": len(req.intent),
             "chars_out": len(out.get("text") or ""), "model": model,
             "latency_ms": out.get("latency_ms"), "app": (req.app_context or "")[:100] or None},
            actor(request),
        )
        return out

    @app.post("/warm")
    def warm_llm() -> dict[str, Any]:
        """The hotkey went down: open the API connection while Rust captures the selection.
        Returns immediately; the warm-up runs on its own thread and never raises."""
        llm = rt().llm
        started = bool(getattr(llm, "warm", None) and llm.warm())
        return {"ok": True, "started": started}

    @app.post("/ingest/manual")
    def ingest_manual(req: IngestManualRequest) -> dict[str, Any]:
        from backend.langgraph.ingest_graph import run_ingest_batch

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
    def personas() -> list[dict[str, Any]]:
        return persona_store.list_personas(rt().db)

    def _persona_call(fn: Any, *args: Any) -> Any:
        try:
            return fn(*args)
        except persona_store.PersonaError as e:
            raise HTTPException(status_code=e.status, detail=str(e)) from e

    @app.post("/personas")
    def create_persona(body: PersonaCreateBody, request: Request) -> dict[str, Any]:
        r = rt()
        p = _persona_call(persona_store.create, r.db, body.name, body.description)
        r.audit_event("persona.create", {"persona": p["name"], "chars": len(p["description"])}, actor(request))
        return p

    @app.put("/personas/{name}")
    def update_persona(name: str, body: PersonaUpdateBody, request: Request) -> dict[str, Any]:
        r = rt()
        p = _persona_call(persona_store.update, r.db, name, body.description)
        r.audit_event("persona.update", {"persona": p["name"], "reset": body.description is None}, actor(request))
        return p

    @app.delete("/personas/{name}")
    def delete_persona(name: str, request: Request) -> dict[str, Any]:
        r = rt()
        out = _persona_call(persona_store.delete, r, name)  # GraphUnavailable → 503, nothing changed
        r.audit_event("persona.delete", {"persona": out["name"], "moved_samples": out["moved_samples"]}, actor(request))
        return out

    @app.get("/config")
    def get_config() -> dict[str, Any]:
        return _config_out(rt().db)

    @app.put("/config")
    def put_config(body: ConfigBody, request: Request) -> dict[str, Any]:
        r = rt()
        values = body.model_dump(exclude_none=True)
        if "active_persona" in values:  # only an existing persona, in its stored spelling
            p = persona_store.find(r.db, values["active_persona"])
            if p is None:
                raise HTTPException(status_code=422, detail=f"No persona called “{values['active_persona']}”.")
            values["active_persona"] = p["name"]
        before = _config_out(r.db)
        r.db.set_config(values)
        after = _config_out(r.db)
        changed = sorted(k for k in after if after[k] != before[k])
        who = actor(request)
        if changed:
            r.audit_event("config.change", {"keys": changed}, who)
        if "capture_enabled" in changed:
            r.audit_event("capture.start" if after["capture_enabled"] else "capture.stop",
                          {"apps": len(after["capture_apps"])}, who)
        if "lock_idle_minutes" in changed:
            auth.set_idle_minutes(after["lock_idle_minutes"])
        if "retention_days" in changed and after["retention_days"]:
            retention.purge(r)
        return after

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

    os.umask(0o077)  # every file the backend creates (SQLite -wal/-shm, tokens, logs) is 0600
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # "constraint already exists" etc. are INFO-level server notifications — just noise here
    logging.getLogger("neo4j.notifications").setLevel(logging.WARNING)
    from backend.daemon.watchdog import start_parent_watchdog

    start_parent_watchdog()
    settings = get_settings()
    file_hygiene.harden(settings.polytone_data_dir, [REPO_ROOT / ".env", settings.gmail_token_path])
    rt = build_runtime(settings, score_async=True)
    set_runtime(rt)
    uvicorn.run(create_app(rt), host="127.0.0.1", port=settings.polytone_api_port, log_level="info")
    rt.close()


if __name__ == "__main__":
    main()
