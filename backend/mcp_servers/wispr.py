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
   ``~/Library/Application Support/Wispr Flow`` (``flow.sqlite``, WAL mode). The DB + -wal are
   APFS-cloned to a temp dir and the copy is opened ``mode=ro`` (opening the live file, even
   read-only, writes its -shm). The schema is inspected dynamically and a raw/ASR text column
   (``asrText`` …) is preferred — never the formatted one; only named columns are selected (never
   the audio/screenshot blobs). Incremental by ``rowid`` (insert order; cursor
   ``wispr.history_rowid`` in SQLite config, migrated from the old ``wispr.history_last_ts``).
   ``backend.mcp_servers.wispr_live`` runs the same read continuously in the daemon.

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
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.ingestion.text import tokenize
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
CURSOR_KEY = "wispr.history_rowid"  # rowid = insert order (see fetch_rows)
OLD_CURSOR_KEY = "wispr.history_last_ts"  # pre-rowid cursor; migrated once, still updated
SPOKEN_PERSONA = "Default"
PENDING_MAX_S = 900.0  # how long a text-less, status-less recent row is re-checked
FILLERS = frozenset({"um", "uh", "umm", "uhh", "uhm", "erm", "er", "ah", "hmm", "hm", "mm", "mhm", "eh", "oh"})
# logical name → History column. ONLY these are ever selected (plus rowid + text/ts/id).
READER_COLS = {
    "status": "status", "archived": "isArchived", "app": "app", "url": "url", "duration": "duration",
    # what Wispr inserted into the app — never learned, only matched against typed messages:
    "pasted": "pastedText", "server_final": "serverFinalizedText", "formatted": "formattedText",
}
INSERTED_KEYS = ("pasted", "server_final", "formatted")


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
    # Wispr's own history file first, then by size
    return sorted(found, key=lambda p: (p.name != "flow.sqlite", -p.stat().st_size))


def file_signature(db_path: Path) -> tuple:
    """(mtime_ns, size) of the DB and its -wal: changes on every Wispr commit. -shm is ignored
    (Wispr touches it on reads too). Missing files count as ``None``."""
    out: list[Any] = []
    for suffix in ("", "-wal"):
        try:
            st = os.stat(str(db_path) + suffix)
            out.append((st.st_mtime_ns, st.st_size))
        except OSError:
            out.append(None)
    return tuple(out)


def _clone(src: Path, dst: Path) -> None:
    if sys.platform == "darwin":
        r = subprocess.run(["cp", "-c", str(src), str(dst)], capture_output=True, check=False)
        if r.returncode == 0:
            return
    shutil.copy2(src, dst)


def _snapshot(db_path: Path, dest_dir: Path, attempts: int = 3) -> Path:
    """Copy the DB + -wal without touching the original (APFS clone when possible: ~ms even for a
    1 GB file). The -shm is NOT copied: the copy is the only connection, so SQLite rebuilds the
    WAL index from the -wal itself (a copied -shm could describe frames the copied -wal lacks).
    The DB and -wal are copied separately, so a Wispr commit/checkpoint in between could tear the
    pair: the signature is compared before/after and the copy retried."""
    out = dest_dir / db_path.name
    for _ in range(attempts):
        before = file_signature(db_path)
        for suffix in ("", "-wal"):
            src, dst = Path(str(db_path) + suffix), Path(str(out) + suffix)
            dst.unlink(missing_ok=True)
            if src.exists():
                _clone(src, dst)
        if file_signature(db_path) == before:
            break
    return out


@contextmanager
def open_snapshot(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Read-only connection to a fresh snapshot of ``db_path`` (deleted on exit)."""
    with tempfile.TemporaryDirectory(prefix="polytone-wispr-") as tmp:
        snap = _snapshot(db_path, Path(tmp))
        conn = _connect_ro(snap)
        try:
            yield conn
        finally:
            conn.close()


def _connect_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def inspect_schema(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """Pick the table + raw-text column holding dictation history. None if nothing fits.

    Also maps the optional columns the incremental reader selects (``cols``: logical name →
    real column). Only these named columns are ever SELECTed — never ``*`` and never the blob
    columns (``audio``, ``screenshot``, ``additionalContext``, …), which hold most of the GB."""
    best: tuple[int, dict[str, Any]] | None = None
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    for t in tables:
        info = list(conn.execute(f"PRAGMA table_info({_q(t)})"))
        cols = [r[1] for r in info]
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
        blob = {r[1] for r in info if str(r[2]).upper() in ("BLOB", "JSON", "JSONB")}
        mapped = {"text": text_col, "ts": ts, "id": ident}
        for logical, real in READER_COLS.items():
            c = lower.get(real.lower())
            if c and c not in blob:
                mapped.setdefault(logical, c)
        cand = {
            "table": t, "text_col": text_col, "ts_col": ts, "id_col": ident,
            "status_col": lower.get("status"), "archived_col": lower.get("isarchived"),
            "app_col": lower.get("app"), "polished_cols_ignored": [c for c in cols if _is_polished(c)],
            "cols": {k: v for k, v in mapped.items() if v},
        }
        if best is None or score > best[0]:
            best = (score, cand)
    return best[1] if best and best[0] > 0 else None


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


@dataclass
class HistoryRow:
    """One Wispr ``History`` row. ``text`` is the RAW transcript (asrText) — the only thing
    learned. ``inserted`` holds what Wispr put into the target app (pastedText /
    serverFinalizedText / formattedText): used ONLY to recognise Wispr output inside typed
    messages, never ingested and never stored in provenance."""

    rowid: int
    text: str
    status: str | None = None
    timestamp: str | None = None
    transcript_id: str | None = None
    app: str | None = None
    url: str | None = None
    duration: float | None = None
    archived: bool = False
    inserted: tuple[str, ...] = ()

    @property
    def epoch(self) -> float | None:
        return parse_ts(self.timestamp)

    @property
    def is_pending(self) -> bool:
        """No text and no status yet: possibly a row Wispr inserts first and fills in later."""
        return not self.text.strip() and not (self.status or "").strip()


def parse_ts(stamp: Any) -> float | None:
    """Wispr's ``'2026-10-31 07:24:01.031 +00:00'`` (or ISO / epoch) → epoch seconds."""
    if stamp is None:
        return None
    if isinstance(stamp, (int, float)):
        return float(stamp) / (1000.0 if stamp > 1e11 else 1.0)
    s = str(stamp).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f %z", "%Y-%m-%d %H:%M:%S %z"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(s.replace(" +", "+").replace(" -", "-"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp()


def fetch_rows(
    conn: sqlite3.Connection, schema: dict[str, Any], *, after_rowid: int = 0,
    rowids: Iterable[int] | None = None, cap: int = LOCAL_CAP, since_ts: str | None = None,
) -> list[HistoryRow]:
    """Rows with ``rowid > after_rowid`` (insert order — Wispr's ``timestamp`` is the dictation
    START, rows are written at the END, so it is not monotonic) or exactly ``rowids``.

    ``since_ts`` additionally keeps only rows whose timestamp is ≥ it (string compare; pass
    ``wispr_ts(...)``). Cheap: ``timestamp`` sits before the audio/screenshot blobs in each row,
    whereas reading a column after them walks the blob's overflow pages (~5ms/row on a real DB)."""
    cols = schema["cols"]
    names = list(cols)
    sel = ", ".join(["rowid"] + [_q(cols[n]) for n in names])
    sql = f"SELECT {sel} FROM {_q(schema['table'])} WHERE "
    if rowids is not None:
        ids = sorted({int(r) for r in rowids})
        if not ids:
            return []
        sql += f"rowid IN ({','.join('?' * len(ids))}) ORDER BY rowid"
        args: list[Any] = ids
    else:
        args = [int(after_rowid)]
        sql += "rowid > ?"
        if since_ts and schema.get("ts_col"):
            sql += f" AND {_q(schema['ts_col'])} >= ?"
            args.append(since_ts)
        sql += f" ORDER BY rowid LIMIT {int(cap)}"
    out: list[HistoryRow] = []
    for raw in conn.execute(sql, args):
        rec = dict(zip(["rowid"] + names, raw, strict=True))
        inserted: list[str] = []
        for k in INSERTED_KEYS:
            v = rec.get(k)
            if isinstance(v, str) and v.strip() and v.strip() not in inserted:
                inserted.append(v.strip())
        dur = rec.get("duration")
        out.append(HistoryRow(
            rowid=int(rec["rowid"]),
            text=str(rec.get("text") or "").strip(),
            status=None if rec.get("status") is None else str(rec["status"]),
            timestamp=None if rec.get("ts") is None else str(rec["ts"]),
            transcript_id=None if rec.get("id") is None else str(rec["id"]),
            app=rec.get("app") or None, url=rec.get("url") or None,
            duration=float(dur) if isinstance(dur, (int, float)) else None,
            archived=bool(rec.get("archived") or 0),
            inserted=tuple(inserted),
        ))
    return out


def max_rowid(conn: sqlite3.Connection, schema: dict[str, Any]) -> int:
    return int(conn.execute(f"SELECT COALESCE(MAX(rowid), 0) FROM {_q(schema['table'])}").fetchone()[0])


def newest_ts(conn: sqlite3.Connection, schema: dict[str, Any]) -> str | None:
    ts = schema.get("ts_col")
    if not ts:
        return None
    r = conn.execute(f"SELECT {_q(ts)} FROM {_q(schema['table'])} ORDER BY rowid DESC LIMIT 1").fetchone()
    return None if r is None or r[0] is None else str(r[0])


def wispr_ts(epoch: float) -> str:
    """Epoch → Wispr's timestamp text (``'2026-10-31 07:24:01.031 +00:00'``, UTC)."""
    dt = datetime.fromtimestamp(epoch, UTC)
    return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d} +00:00"


def is_filler_only(text: str) -> bool:
    toks = tokenize(text)
    return bool(toks) and all(t in FILLERS for t in toks)


def skip_reason(row: HistoryRow) -> str | None:
    """Why a row is not learned from (None → learn its raw transcript as-is).

    Archived rows ARE learned: archiving only hides a dictation in Wispr's UI, it is still the
    user's speech. Filler-only transcripts ("um", "uh um ok") are dropped — real transcripts are
    never "polished"; the firewall's own length rules apply to the rest."""
    if not row.text:
        return "no raw transcript"
    if (row.status or "").strip().lower() in SKIP_STATUSES:
        return f"status {row.status}"
    if is_filler_only(row.text):
        return "filler only"
    return None


def row_item(row: HistoryRow, field: str = "asrText") -> SourceItem:
    """Ingest item for a row's RAW transcript. Spoken text is a different register from typed
    text: it is learned under the global ``Default`` persona and tagged ``kind="spoken"``."""
    prov = outbound(
        "wispr", raw_transcript=True, kind="spoken", origin="wispr_local_history", field=field,
        transcript_id=row.transcript_id, rowid=row.rowid, timestamp=row.timestamp, app=row.app,
    )
    for k, v in (("url", row.url), ("duration", row.duration)):
        if v is not None:
            prov[k] = v
    if row.archived:
        prov["archived"] = True
    return {"text": row.text, "provenance": prov, "persona": SPOKEN_PERSONA}


# cursor / pending rows -------------------------------------------------------------------
_read_lock = threading.Lock()  # one incremental read (cursor read → advance) at a time
_pending: dict[tuple[str, int], float] = {}  # (db path, rowid) → monotonic first seen


def load_cursor(db: Any, conn: sqlite3.Connection, schema: dict[str, Any]) -> int:
    """The rowid cursor; migrated once from the old timestamp cursor (``wispr.history_last_ts``)
    as ``MAX(rowid) WHERE timestamp <= old``."""
    v = get_state(db, CURSOR_KEY)
    if v not in (None, ""):
        try:
            return int(v)
        except ValueError:
            log.warning("bad %s=%r; restarting from 0", CURSOR_KEY, v)
    old, ts = get_state(db, OLD_CURSOR_KEY), schema.get("ts_col")
    cur = 0
    if old and ts:
        r = conn.execute(f"SELECT MAX(rowid) FROM {_q(schema['table'])} WHERE {_q(ts)} <= ?", (old,)).fetchone()
        cur = int(r[0] or 0)
        log.info("wispr: migrated timestamp cursor %s → rowid %d", old, cur)
    set_state(db, **{CURSOR_KEY: str(cur)})
    return cur


def read_increment(
    db_path: Path, db: Any, cap: int = LOCAL_CAP, conn: sqlite3.Connection | None = None,
) -> tuple[list[SourceItem], list[HistoryRow], dict[str, Any]]:
    """New learnable items since the persisted rowid cursor; advances the cursor.

    Returns (items, every row read — for the Wispr insertion index —, info). Rows that have no
    text and no status yet and are recent are re-checked on later reads (``_pending``) in case
    Wispr fills them in after inserting."""
    with _read_lock:
        if conn is None:
            with open_snapshot(db_path) as c:
                return _read_increment(db_path, db, cap, c)
        return _read_increment(db_path, db, cap, conn)


def _read_increment(db_path: Path, db: Any, cap: int, conn: sqlite3.Connection):
    schema = inspect_schema(conn)
    if schema is None:
        return [], [], {"db": str(db_path), "error": "no table with a raw transcript column"}
    cursor = load_cursor(db, conn, schema)
    rows = fetch_rows(conn, schema, after_rowid=cursor, cap=cap)
    key = str(db_path)
    now_m, now_w = time.monotonic(), time.time()
    pend_ids = [r for (p, r), t0 in list(_pending.items()) if p == key]
    rechecked = fetch_rows(conn, schema, rowids=pend_ids) if pend_ids else []
    for r in rechecked:
        if not r.is_pending or now_m - _pending.get((key, r.rowid), now_m) > PENDING_MAX_S:
            _pending.pop((key, r.rowid), None)
    for rid in set(pend_ids) - {r.rowid for r in rechecked}:  # deleted
        _pending.pop((key, rid), None)
    for r in rows:
        ep = r.epoch
        if r.is_pending and ep is not None and now_w - ep < PENDING_MAX_S:
            _pending[(key, r.rowid)] = now_m
    seen = rows + [r for r in rechecked if not r.is_pending]
    items, skipped = [], {}
    for r in seen:
        why = skip_reason(r)
        if why is None:
            items.append(row_item(r, schema["text_col"]))
        elif not r.is_pending:
            skipped[why] = skipped.get(why, 0) + 1
    new_cursor = rows[-1].rowid if rows else cursor
    newest_ts = max((r.timestamp for r in rows if r.timestamp), default=None)
    if new_cursor != cursor:
        state = {CURSOR_KEY: str(new_cursor)}
        old = get_state(db, OLD_CURSOR_KEY)
        if newest_ts and (old is None or newest_ts > old):
            state[OLD_CURSOR_KEY] = newest_ts  # kept for older builds; the rowid cursor rules
        set_state(db, **state)
    info = {
        "db": str(db_path), "table": schema["table"], "text_col": schema["text_col"],
        "rows": len(items), "rows_read": len(rows), "cursor": new_cursor, "newest": newest_ts,
        "more": len(rows) >= cap, "skipped": skipped, "pending": sum(1 for p, _ in _pending if p == key),
        "ignored_cols": schema["polished_cols_ignored"],
    }
    return items, seen, info


def read_history(db_path: Path, after_rowid: int = 0, cap: int = LOCAL_CAP) -> tuple[list[SourceItem], dict]:
    """Learnable raw transcripts with ``rowid > after_rowid`` (no cursor state involved)."""
    with open_snapshot(db_path) as conn:
        schema = inspect_schema(conn)
        if schema is None:
            return [], {"db": str(db_path), "error": "no table with a raw transcript column"}
        rows = fetch_rows(conn, schema, after_rowid=after_rowid, cap=cap)
    items = [row_item(r, schema["text_col"]) for r in rows if skip_reason(r) is None]
    return items, {"db": str(db_path), "table": schema["table"], "text_col": schema["text_col"],
                   "rows": len(items), "cursor": rows[-1].rowid if rows else after_rowid,
                   "ignored_cols": schema["polished_cols_ignored"]}


def collect_local(settings: Any, cap: int = LOCAL_CAP, db: Any = None) -> tuple[list[SourceItem], dict]:
    dbs = find_history_dbs()
    if not dbs:
        return [], {"skipped": "no local Wispr Flow history found"}
    db = db or state_db(settings)
    errors = []
    for path in dbs:
        try:
            items, rows, info = read_increment(path, db, cap)
        except (sqlite3.Error, OSError) as e:
            errors.append(f"{path.name}: {e}")
            continue
        if "error" in info:
            errors.append(f"{path.name}: {info['error']}")
            continue
        try:  # the on-demand path feeds the typed-capture insertion index too
            from backend.mcp_servers import wispr_live

            wispr_live.INSERTIONS.add_rows(rows)
        except Exception as e:  # noqa: BLE001
            log.debug("wispr insertion index: %s", e)
        return items, info
    return [], {"error": "; ".join(errors) or "no usable Wispr history DB"}


# =========================================================================== source hook
def is_enabled(settings: Any) -> bool:
    return ws_configured(settings) or bool(find_history_dbs())


def status(settings: Any) -> str:
    parts = []
    if ws_configured(settings):
        parts.append("websocket configured")
    live = _live_status()
    if live:
        parts.append(live)
    else:
        dbs = find_history_dbs()
        if dbs:
            parts.append(f"local history: {dbs[0].name}")
    if not parts:
        return "needs credentials: set WISPR_WS_URL + WISPR_API_KEY (no local Wispr Flow history found)"
    return ("live (" if live else "ready (") + ", ".join(parts) + ")"


def _live_status() -> str | None:
    try:
        from backend.mcp_servers import wispr_live
    except Exception:  # noqa: BLE001
        return None
    return wispr_live.status_text()


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
        "history_rowid": get_state(rt.db, CURSOR_KEY),
        "history_last_ts": get_state(rt.db, OLD_CURSOR_KEY),
        "live": _live_status(),
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
