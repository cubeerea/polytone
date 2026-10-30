"""SQLite storage for Polytone (see CONTRACTS.md).

Tables
------
- ``config``      key/value settings (active_persona, hotkey, inject_mode).
- ``drafts``      every generated draft (NEVER fed back into the graph).
- ``samples``     ingested *human* text, unique by sha256, with provenance.
- ``ingest_runs`` one row per ingestion run of a source.

Usage
-----
Two equivalent styles are supported:

* ``Database(path)`` instance methods (preferred for injection / tests), and
* module-level plain functions (``get_config()``, ``insert_draft(...)`` ...) which
  delegate to a process-wide default ``Database``. Call ``configure(path)`` to point
  the default at a different file (tests do this with a temp path); otherwise it
  uses ``get_settings().sqlite_path``.

Thread-safety: every call opens its own short-lived connection (WAL mode), so the
functions can be used from any thread (FastAPI threadpool, background scorers).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_CONFIG: dict[str, str] = {
    "active_persona": "Default",
    "hotkey": "CmdOrCtrl+Shift+P",
    "inject_mode": "paste",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
    draft_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    persona TEXT NOT NULL,
    intent TEXT NOT NULL,
    text TEXT NOT NULL,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    constraints_json TEXT,
    score_json TEXT,
    sha256 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_drafts_created ON drafts(created_at);
CREATE INDEX IF NOT EXISTS idx_drafts_sha ON drafts(sha256);
CREATE TABLE IF NOT EXISTS samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL,
    persona TEXT NOT NULL,
    text TEXT NOT NULL,
    sha256 TEXT NOT NULL UNIQUE,
    provenance_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_samples_persona ON samples(persona);
CREATE INDEX IF NOT EXISTS idx_samples_source ON samples(source);
CREATE TABLE IF NOT EXISTS ingest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    accepted INTEGER NOT NULL DEFAULT 0,
    rejected INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ok'
);
CREATE INDEX IF NOT EXISTS idx_runs_source ON ingest_runs(source);
"""


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def sha256_text(text: str) -> str:
    """Canonical hash used for drafts AND samples (stripped UTF-8)."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def _loads(s: str | None) -> Any:
    if not s:
        return None
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return None


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_lock = threading.Lock()
        self._initialized = False
        self.init()

    # ------------------------------------------------------------------ plumbing
    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:  # commit / rollback
                yield conn
        finally:
            conn.close()

    def init(self) -> None:
        with self._init_lock:
            if self._initialized:
                return
            with self.connect() as c:
                c.execute("PRAGMA journal_mode=WAL")
                c.executescript(_SCHEMA)
                for k, v in DEFAULT_CONFIG.items():
                    c.execute("INSERT OR IGNORE INTO config(k, v) VALUES (?, ?)", (k, v))
            self._initialized = True

    # ------------------------------------------------------------------ config
    def get_config(self) -> dict[str, str]:
        with self.connect() as c:
            rows = c.execute("SELECT k, v FROM config").fetchall()
        cfg = dict(DEFAULT_CONFIG)
        cfg.update({r["k"]: r["v"] for r in rows})
        return cfg

    def set_config(self, values: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, str]:
        """Upsert keys (``None`` values are ignored). Returns the full config."""
        merged = {**(values or {}), **kwargs}
        with self.connect() as c:
            for k, v in merged.items():
                if v is None:
                    continue
                c.execute(
                    "INSERT INTO config(k, v) VALUES (?, ?) "
                    "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                    (k, str(v)),
                )
        return self.get_config()

    # ------------------------------------------------------------------ drafts
    def insert_draft(
        self,
        *,
        persona: str,
        intent: str,
        text: str,
        latency_ms: int = 0,
        constraints: dict | None = None,
        score: dict | None = None,
        draft_id: str | None = None,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        row = {
            "draft_id": draft_id or uuid.uuid4().hex,
            "created_at": created_at or utcnow_iso(),
            "persona": persona,
            "intent": intent,
            "text": text,
            "latency_ms": int(latency_ms),
            "constraints_json": json.dumps(constraints) if constraints is not None else None,
            "score_json": json.dumps(score) if score is not None else None,
            "sha256": sha256_text(text),
        }
        with self.connect() as c:
            c.execute(
                "INSERT INTO drafts(draft_id, created_at, persona, intent, text, latency_ms,"
                " constraints_json, score_json, sha256) VALUES (:draft_id, :created_at, :persona,"
                " :intent, :text, :latency_ms, :constraints_json, :score_json, :sha256)",
                row,
            )
        return self._draft_out(row)

    @staticmethod
    def _draft_out(row: Any) -> dict[str, Any]:
        r = dict(row)
        return {
            "draft_id": r["draft_id"],
            "created_at": r["created_at"],
            "persona": r["persona"],
            "intent": r["intent"],
            "text": r["text"],
            "latency_ms": r["latency_ms"],
            "constraints": _loads(r.get("constraints_json")),
            "score": _loads(r.get("score_json")),
            "sha256": r["sha256"],
        }

    def get_draft(self, draft_id: str) -> dict[str, Any] | None:
        with self.connect() as c:
            row = c.execute("SELECT * FROM drafts WHERE draft_id = ?", (draft_id,)).fetchone()
        return self._draft_out(row) if row else None

    def list_drafts(self, limit: int = 50, persona: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM drafts"
        args: list[Any] = []
        if persona:
            q += " WHERE persona = ?"
            args.append(persona)
        q += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
        args.append(int(limit))
        with self.connect() as c:
            rows = c.execute(q, args).fetchall()
        return [self._draft_out(r) for r in rows]

    def update_draft_score(self, draft_id: str, score: dict | None) -> None:
        with self.connect() as c:
            c.execute(
                "UPDATE drafts SET score_json = ? WHERE draft_id = ?",
                (json.dumps(score) if score is not None else None, draft_id),
            )

    def all_draft_texts(self) -> list[str]:
        with self.connect() as c:
            return [r[0] for r in c.execute("SELECT text FROM drafts").fetchall()]

    def draft_hashes(self) -> set[str]:
        with self.connect() as c:
            return {r[0] for r in c.execute("SELECT sha256 FROM drafts").fetchall()}

    def scored_drafts(self) -> list[tuple[str, dict]]:
        """(created_at, score) for every draft with a score, oldest first."""
        with self.connect() as c:
            rows = c.execute(
                "SELECT created_at, score_json FROM drafts WHERE score_json IS NOT NULL"
                " ORDER BY created_at ASC"
            ).fetchall()
        out = []
        for r in rows:
            s = _loads(r["score_json"])
            if isinstance(s, dict):
                out.append((r["created_at"], s))
        return out

    # ------------------------------------------------------------------ samples
    def insert_sample(
        self,
        *,
        text: str,
        source: str,
        persona: str,
        provenance: dict | None = None,
        created_at: str | None = None,
    ) -> int | None:
        """Insert a human sample. Returns the row id, or None if the sha already exists."""
        with self.connect() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO samples(created_at, source, persona, text, sha256,"
                " provenance_json) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    created_at or utcnow_iso(),
                    source,
                    persona,
                    text,
                    sha256_text(text),
                    json.dumps(provenance or {}),
                ),
            )
            return cur.lastrowid if cur.rowcount else None

    def list_samples(
        self, persona: str | None = None, limit: int | None = 200, source: str | None = None
    ) -> list[dict[str, Any]]:
        q = "SELECT * FROM samples"
        where, args = [], []
        if persona:
            where.append("persona = ?")
            args.append(persona)
        if source:
            where.append("source = ?")
            args.append(source)
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY created_at DESC, id DESC"
        if limit is not None:
            q += " LIMIT ?"
            args.append(int(limit))
        with self.connect() as c:
            rows = c.execute(q, args).fetchall()
        return [
            {
                "id": r["id"],
                "created_at": r["created_at"],
                "source": r["source"],
                "persona": r["persona"],
                "text": r["text"],
                "sha256": r["sha256"],
                "provenance": _loads(r["provenance_json"]) or {},
            }
            for r in rows
        ]

    def sample_exists(self, sha: str) -> bool:
        with self.connect() as c:
            return c.execute("SELECT 1 FROM samples WHERE sha256 = ?", (sha,)).fetchone() is not None

    def count_samples(self, source: str | None = None) -> int:
        with self.connect() as c:
            if source:
                return c.execute("SELECT COUNT(*) FROM samples WHERE source = ?", (source,)).fetchone()[0]
            return c.execute("SELECT COUNT(*) FROM samples").fetchone()[0]

    def sample_personas(self) -> list[str]:
        with self.connect() as c:
            return [r[0] for r in c.execute("SELECT DISTINCT persona FROM samples ORDER BY persona")]

    # ------------------------------------------------------------------ ingest runs
    def record_ingest_run(
        self,
        *,
        source: str,
        started_at: str,
        finished_at: str | None = None,
        accepted: int = 0,
        rejected: int = 0,
        status: str = "ok",
    ) -> int:
        with self.connect() as c:
            cur = c.execute(
                "INSERT INTO ingest_runs(source, started_at, finished_at, accepted, rejected, status)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (source, started_at, finished_at or utcnow_iso(), accepted, rejected, status),
            )
            return int(cur.lastrowid)

    def list_ingest_runs(self, source: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        q = "SELECT * FROM ingest_runs"
        args: list[Any] = []
        if source:
            q += " WHERE source = ?"
            args.append(source)
        q += " ORDER BY started_at DESC, id DESC LIMIT ?"
        args.append(int(limit))
        with self.connect() as c:
            return [dict(r) for r in c.execute(q, args).fetchall()]


# ---------------------------------------------------------------------- module API
_default: Database | None = None
_default_lock = threading.Lock()


def configure(path: str | Path | None = None) -> Database:
    """(Re)point the module-level default database. ``None`` → settings.sqlite_path."""
    global _default
    if path is None:
        from backend.config import get_settings

        path = get_settings().sqlite_path
    with _default_lock:
        _default = Database(path)
    return _default


def get_db() -> Database:
    if _default is None:
        return configure()
    return _default


def get_config() -> dict[str, str]:
    return get_db().get_config()


def set_config(values: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, str]:
    return get_db().set_config(values, **kwargs)


def insert_draft(**kwargs: Any) -> dict[str, Any]:
    return get_db().insert_draft(**kwargs)


def get_draft(draft_id: str) -> dict[str, Any] | None:
    return get_db().get_draft(draft_id)


def list_drafts(limit: int = 50, persona: str | None = None) -> list[dict[str, Any]]:
    return get_db().list_drafts(limit, persona)


def update_draft_score(draft_id: str, score: dict | None) -> None:
    get_db().update_draft_score(draft_id, score)


def all_draft_texts() -> list[str]:
    return get_db().all_draft_texts()


def draft_hashes() -> set[str]:
    return get_db().draft_hashes()


def insert_sample(**kwargs: Any) -> int | None:
    return get_db().insert_sample(**kwargs)


def list_samples(persona: str | None = None, limit: int | None = 200, source: str | None = None):
    return get_db().list_samples(persona, limit, source)


def sample_exists(sha: str) -> bool:
    return get_db().sample_exists(sha)


def record_ingest_run(**kwargs: Any) -> int:
    return get_db().record_ingest_run(**kwargs)


def list_ingest_runs(source: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    return get_db().list_ingest_runs(source, limit)
