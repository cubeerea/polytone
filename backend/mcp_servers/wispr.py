"""Wispr Flow source + MCP server — RAW dictation transcripts only.

Two paths, both yielding ``{"text", "provenance": {"source": "wispr", "raw_transcript": True}}``:

1. **WebSocket** (``WISPR_WS_URL`` + ``WISPR_API_KEY``). No public Wispr developer WebSocket API
   could be verified, so the client is generic: it connects with ``Authorization: Bearer <key>``
   (also ``X-API-Key``), reconnects with exponential backoff, and parses JSON messages with
   tolerant field names (``raw_text``/``rawText``/``asr_text``/``asrText``/``raw_transcript``/
   ``transcript``/``text``, optionally nested under ``data``/``payload``/``transcript``/``result``).
   Any formatted / AI-polished field (``formatted*``, ``polished*``, ``edited*``, ``toneMatched*``,
   ``pasted*``) is ignored; a message that only has polished text is dropped. Interim results
   (``is_final: false`` / ``type: "partial"``) are dropped.
2. **Local history fallback** (macOS): Wispr Flow is an Electron app that keeps an SQLite DB in
   ``~/Library/Application Support/Wispr Flow`` (or ``com.electron.wispr-flow``). The DB is
   copied (with its -wal/-shm) to a temp dir and opened ``mode=ro``; the schema is inspected
   dynamically and a raw/ASR text column (``asrText`` …) is preferred — never the formatted one.
   Incremental by the timestamp column (cursor ``wispr.history_last_ts`` in SQLite config).

Env: ``WISPR_HISTORY_DIRS`` (os.pathsep-separated) overrides the search dirs;
``POLYTONE_WISPR_LOCAL=0`` disables the local fallback; ``WISPR_WS_COLLECT_SECONDS`` (default 3)
bounds how long a one-shot ``collect()`` listens on the WebSocket.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from backend.mcp_servers._common import (
    SourceItem,
    get_state,
    make_server,
    outbound,
    set_state,
    state_db,
    truthy_env,
)

log = logging.getLogger(__name__)

# ---- field-name policy (shared by WS + local DB) -------------------------------------------
RAW_FIELDS = (
    "raw_text", "rawText", "raw_transcript", "rawTranscript", "asr_text", "asrText",
    "defaultAsrText", "transcript", "text",
)
POLISHED_MARKERS = ("formatted", "polish", "edited", "tonematched", "pasted", "corrected", "refined", "ai_")
NESTED_KEYS = ("data", "payload", "transcript", "result", "message")

DEFAULT_HISTORY_DIRS = (
    "~/Library/Application Support/Wispr Flow",
    "~/Library/Application Support/com.electron.wispr-flow",
)
DB_SUFFIXES = (".sqlite", ".sqlite3", ".db")
LOCAL_CAP = 2000
SKIP_STATUSES = {"dismissed", "error", "failed", "cancelled", "canceled", "discarded"}


def _is_polished(name: str) -> bool:
    n = name.lower()
    return any(m in n for m in POLISHED_MARKERS)


# =========================================================================== WebSocket
def parse_message(raw: str | bytes | dict[str, Any]) -> dict[str, Any] | None:
    """Return ``{"text", "id"?, "timestamp"?}`` for a final RAW transcript message, else None."""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, dict):
        return None
    msg: dict[str, Any] = raw
    for _ in range(3):  # unwrap envelopes like {"type": "...", "data": {...}}
        if msg.get("is_final") is False or msg.get("isFinal") is False or msg.get("final") is False:
            return None
        if str(msg.get("type", "")).lower() in ("partial", "interim", "error", "ping", "pong", "heartbeat"):
            return None
        for key in RAW_FIELDS:
            v = msg.get(key)
            if isinstance(v, str) and v.strip() and not _is_polished(key):
                return {
                    "text": v.strip(),
                    "id": msg.get("id") or msg.get("transcript_id") or msg.get("transcriptEntityId"),
                    "timestamp": msg.get("timestamp") or msg.get("created_at") or msg.get("createdAt"),
                }
        nested = next((msg[k] for k in NESTED_KEYS if isinstance(msg.get(k), dict)), None)
        if nested is None:
            return None
        msg = nested
    return None


def _ws_item(parsed: dict[str, Any]) -> SourceItem:
    return {
        "text": parsed["text"],
        "provenance": outbound(
            "wispr", raw_transcript=True, origin="wispr_ws", transcript_id=parsed.get("id"),
            timestamp=parsed.get("timestamp"),
        ),
    }


def _connect(url: str, api_key: str, open_timeout: float):
    from websockets.asyncio.client import connect

    headers = {"Authorization": f"Bearer {api_key}", "X-API-Key": api_key} if api_key else {}
    return connect(url, additional_headers=headers, open_timeout=open_timeout, max_size=2**22)


async def stream_transcripts(
    url: str,
    api_key: str,
    on_item: Callable[[SourceItem], Any],
    *,
    stop: asyncio.Event | None = None,
    duration: float | None = None,
    max_items: int | None = None,
    backoff_initial: float = 0.5,
    backoff_max: float = 30.0,
    max_attempts: int | None = None,
    open_timeout: float = 5.0,
) -> dict[str, Any]:
    """Receive raw transcripts, reconnecting with exponential backoff.

    Stops when ``stop`` is set, ``duration`` seconds elapse, ``max_items`` are received, or
    ``max_attempts`` consecutive connection failures happen. Returns counters.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + duration if duration else None
    stop = stop or asyncio.Event()
    stats = {"received": 0, "ignored": 0, "connects": 0, "failures": 0, "last_error": None}
    delay = backoff_initial
    attempts = 0

    def remaining() -> float | None:
        return None if deadline is None else deadline - loop.time()

    while not stop.is_set() and (deadline is None or remaining() > 0):
        try:
            async with _connect(url, api_key, open_timeout) as ws:
                stats["connects"] += 1
                attempts, delay = 0, backoff_initial
                while not stop.is_set():
                    rem = remaining()
                    if rem is not None and rem <= 0:
                        return stats
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=min(rem, 1.0) if rem else 1.0)
                    except TimeoutError:
                        continue
                    parsed = parse_message(raw)
                    if parsed is None:
                        stats["ignored"] += 1
                        continue
                    res = on_item(_ws_item(parsed))
                    if asyncio.iscoroutine(res):
                        await res
                    stats["received"] += 1
                    if max_items and stats["received"] >= max_items:
                        return stats
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - network errors → backoff + reconnect
            stats["failures"] += 1
            stats["last_error"] = f"{e.__class__.__name__}: {e}"
            attempts += 1
            if max_attempts is not None and attempts >= max_attempts:
                return stats
        rem = remaining()
        wait = delay if rem is None else max(0.0, min(delay, rem))
        if wait:
            try:
                await asyncio.wait_for(stop.wait(), timeout=wait)
            except TimeoutError:
                pass
        delay = min(delay * 2, backoff_max)
    return stats


def ws_configured(settings: Any) -> bool:
    return bool((getattr(settings, "wispr_ws_url", "") or "").strip() and (getattr(settings, "wispr_api_key", "") or "").strip())


def collect_ws(settings: Any, duration: float | None = None, max_items: int | None = None) -> tuple[list[SourceItem], dict]:
    if not ws_configured(settings):
        return [], {"skipped": "WISPR_WS_URL / WISPR_API_KEY not set"}
    duration = duration if duration is not None else float(os.environ.get("WISPR_WS_COLLECT_SECONDS", "3"))
    items: list[SourceItem] = []
    stats = asyncio.run(
        stream_transcripts(
            settings.wispr_ws_url, settings.wispr_api_key, items.append, duration=duration,
            max_items=max_items, max_attempts=3,
        )
    )
    return items, stats


# =========================================================================== local history
def history_dirs() -> list[Path]:
    env = os.environ.get("WISPR_HISTORY_DIRS")
    raw = env.split(os.pathsep) if env else list(DEFAULT_HISTORY_DIRS)
    return [Path(p).expanduser() for p in raw if p]


def find_history_dbs() -> list[Path]:
    """SQLite files in the Wispr app-support dirs (top level + one level down, backups excluded)."""
    if not truthy_env("POLYTONE_WISPR_LOCAL", True):
        return []
    found: list[Path] = []
    for d in history_dirs():
        if not d.is_dir():
            continue
        cands = list(d.iterdir()) + [p for sub in d.iterdir() if sub.is_dir() and sub.name != "backups"
                                     for p in sub.iterdir()]
        for p in cands:
            if p.is_file() and p.suffix.lower() in DB_SUFFIXES and not p.name.startswith("."):
                try:
                    with p.open("rb") as fh:
                        if fh.read(16) == b"SQLite format 3\x00":
                            found.append(p)
                except OSError:
                    continue
    return sorted(found, key=lambda p: p.stat().st_size, reverse=True)


def _snapshot(db_path: Path, dest_dir: Path) -> Path:
    """Copy the DB (+ -wal/-shm) without touching the original. APFS clone when possible."""
    out = dest_dir / db_path.name
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(db_path) + suffix)
        if not src.exists():
            continue
        dst = Path(str(out) + suffix)
        if sys.platform == "darwin":
            r = subprocess.run(["cp", "-c", str(src), str(dst)], capture_output=True, check=False)
            if r.returncode == 0:
                continue
        shutil.copy2(src, dst)
    return out


def inspect_schema(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """Pick the table + raw-text column holding dictation history. None if nothing fits."""
    best: tuple[int, dict[str, Any]] | None = None
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    for t in tables:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({_q(t)})")]
        lower = {c.lower(): c for c in cols}
        text_col = None
        for rank, name in enumerate(RAW_FIELDS):
            c = lower.get(name.lower())
            if c and not _is_polished(c):
                text_col, score = c, 100 - rank * 5
                break
        if not text_col:
            continue
        tl = t.lower()
        if tl in ("history", "transcripts", "transcript", "dictations", "dictation_history"):
            score += 50
        elif "history" in tl or "transcript" in tl or "dictation" in tl:
            score += 25
        if not any(n in lower for n in ("asrtext", "asr_text", "raw_text", "rawtext", "raw_transcript")):
            score -= 40  # a bare "text" column is a weak signal
        ts = next((lower[n] for n in ("timestamp", "createdat", "created_at", "time", "date") if n in lower), None)
        ident = next((lower[n] for n in ("transcriptentityid", "transcript_id", "id", "uuid") if n in lower), None)
        cand = {
            "table": t, "text_col": text_col, "ts_col": ts, "id_col": ident,
            "status_col": lower.get("status"), "archived_col": lower.get("isarchived"),
            "app_col": lower.get("app"), "polished_cols_ignored": [c for c in cols if _is_polished(c)],
        }
        if best is None or score > best[0]:
            best = (score, cand)
    return best[1] if best and best[0] > 0 else None


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def read_history(db_path: Path, since: str | None = None, cap: int = LOCAL_CAP) -> tuple[list[SourceItem], dict]:
    """Raw transcripts newer than ``since`` from a Wispr history DB (read-only snapshot)."""
    with tempfile.TemporaryDirectory(prefix="polytone-wispr-") as tmp:
        snap = _snapshot(db_path, Path(tmp))
        conn = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
        try:
            schema = inspect_schema(conn)
            if schema is None:
                return [], {"db": str(db_path), "error": "no table with a raw transcript column"}
            t, c, ts = schema["table"], schema["text_col"], schema["ts_col"]
            cols = [c] + [x for x in (ts, schema["id_col"], schema["app_col"]) if x]
            where = [f"{_q(c)} IS NOT NULL", f"TRIM({_q(c)}) != ''"]
            args: list[Any] = []
            if schema["status_col"]:
                where.append(f"(LOWER(COALESCE({_q(schema['status_col'])}, '')) NOT IN "
                             f"({','.join('?' * len(SKIP_STATUSES))}))")
                args.extend(sorted(SKIP_STATUSES))
            if schema["archived_col"]:
                where.append(f"COALESCE({_q(schema['archived_col'])}, 0) = 0")
            if ts and since:
                where.append(f"{_q(ts)} > ?")
                args.append(since)
            sql = f"SELECT {', '.join(_q(x) for x in cols)} FROM {_q(t)} WHERE {' AND '.join(where)}"
            sql += f" ORDER BY {_q(ts)} ASC" if ts else ""
            sql += f" LIMIT {int(cap)}"
            rows = conn.execute(sql, args).fetchall()
        finally:
            conn.close()
    items: list[SourceItem] = []
    newest = since
    for row in rows:
        rec = dict(zip(cols, row, strict=True))
        stamp = rec.get(ts) if ts else None
        if stamp is not None:
            stamp = str(stamp)
            newest = stamp if newest is None or stamp > newest else newest
        items.append({
            "text": str(rec[c]).strip(),
            "provenance": outbound(
                "wispr", raw_transcript=True, origin="wispr_local_history", field=c,
                transcript_id=rec.get(schema["id_col"]) if schema["id_col"] else None,
                timestamp=stamp, app=rec.get(schema["app_col"]) if schema["app_col"] else None,
            ),
        })
    return items, {"db": str(db_path), "table": t, "text_col": c, "rows": len(items), "newest": newest,
                   "ignored_cols": schema["polished_cols_ignored"]}


def collect_local(settings: Any, cap: int = LOCAL_CAP, db: Any = None) -> tuple[list[SourceItem], dict]:
    dbs = find_history_dbs()
    if not dbs:
        return [], {"skipped": "no local Wispr Flow history found"}
    db = db or state_db(settings)
    since = get_state(db, "wispr.history_last_ts")
    errors = []
    for path in dbs:
        try:
            items, info = read_history(path, since, cap)
        except sqlite3.Error as e:
            errors.append(f"{path.name}: {e}")
            continue
        if "error" in info:
            errors.append(f"{path.name}: {info['error']}")
            continue
        if info.get("newest") and info["newest"] != since:
            set_state(db, **{"wispr.history_last_ts": info["newest"]})
        return items, info
    return [], {"error": "; ".join(errors) or "no usable Wispr history DB"}


# =========================================================================== source hook
def is_enabled(settings: Any) -> bool:
    return ws_configured(settings) or bool(find_history_dbs())


def status(settings: Any) -> str:
    parts = []
    if ws_configured(settings):
        parts.append("websocket configured")
    dbs = find_history_dbs()
    if dbs:
        parts.append(f"local history: {dbs[0].name}")
    if not parts:
        return "needs credentials: set WISPR_WS_URL + WISPR_API_KEY (no local Wispr Flow history found)"
    return "ready (" + ", ".join(parts) + ")"


def collect(settings: Any) -> list[SourceItem]:
    items, info = collect_local(settings)
    if info.get("error"):
        log.warning("wispr local history: %s", info["error"])
    ws_items, ws_info = collect_ws(settings)
    if ws_info.get("last_error"):
        log.warning("wispr websocket: %s", ws_info["last_error"])
    return items + ws_items


# =========================================================================== MCP server
mcp = make_server(
    "polytone-wispr",
    "Ingests the user's RAW Wispr Flow dictation transcripts (never the AI-formatted text).",
)


def _ingest(items: list[SourceItem]) -> dict[str, Any]:
    from backend.langgraph.ingest_graph import run_ingest_batch

    return run_ingest_batch(items, "wispr")


@mcp.tool()
def sync_history(max_items: int = LOCAL_CAP) -> dict[str, Any]:
    """Ingest new raw transcripts from the local Wispr Flow history DB (read-only copy)."""
    from backend.runtime import get_runtime

    items, info = collect_local(get_runtime().settings, cap=max(1, min(int(max_items), 10000)))
    return {"source_info": info, **_ingest(items)}


@mcp.tool()
async def listen(seconds: float = 30.0, max_items: int = 100) -> dict[str, Any]:
    """Listen on the Wispr WebSocket for up to ``seconds`` and ingest raw transcripts."""
    from backend.runtime import get_runtime

    s = get_runtime().settings
    if not ws_configured(s):
        return {"ok": False, "status": status(s)}
    items: list[SourceItem] = []
    stats = await stream_transcripts(s.wispr_ws_url, s.wispr_api_key, items.append, duration=seconds,
                                     max_items=max_items, max_attempts=5)
    return {"ok": True, "ws": stats, **await asyncio.to_thread(_ingest, items)}


@mcp.tool(name="status")
def status_tool() -> dict[str, Any]:
    """Configured paths (WebSocket / local history) and the incremental cursor."""
    from backend.runtime import get_runtime

    rt = get_runtime()
    return {
        "enabled": is_enabled(rt.settings), "status": status(rt.settings),
        "websocket": ws_configured(rt.settings), "local_dbs": [str(p) for p in find_history_dbs()],
        "history_last_ts": get_state(rt.db, "wispr.history_last_ts"),
        "items_ingested": rt.db.count_samples(source="wispr"),
    }


def run_forever(settings: Any = None) -> None:
    """Continuously stream WebSocket transcripts into ``run_ingest`` (Ctrl+C to stop)."""
    from backend.langgraph.ingest_graph import run_ingest
    from backend.runtime import get_runtime

    rt = get_runtime()
    settings = settings or rt.settings
    if not ws_configured(settings):
        raise SystemExit(status(settings))

    def on_item(item: SourceItem) -> None:
        r = run_ingest(item["text"], "wispr", None, item["provenance"], runtime=rt)
        log.info("wispr transcript: accepted=%s %s", r["accepted"], r["reasons"])

    asyncio.run(stream_transcripts(settings.wispr_ws_url, settings.wispr_api_key, on_item))


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "serve"
    if cmd == "serve":
        mcp.run("stdio")
    elif cmd == "listen":
        logging.basicConfig(level=logging.INFO)
        run_forever()
    elif cmd == "sync":
        print(json.dumps(sync_history(), indent=2, default=str))
    elif cmd == "status":
        print(json.dumps(status_tool(), indent=2, default=str))
    else:
        raise SystemExit("usage: python -m backend.mcp_servers.wispr [serve|listen|sync|status]")


if __name__ == "__main__":
    main()
