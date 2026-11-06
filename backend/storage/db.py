"""SQLite storage for Polytone (see CONTRACTS.md).

Tables
------
- ``config``      key/value settings (active_persona, hotkey, inject_mode, capture_enabled,
                  capture_apps). Values are strings; bools/lists are stored as JSON
                  (``"true"``, ``["com.apple.mail", ...]``) — use ``capture_config()`` to read them.
- ``drafts``      every generated draft (NEVER fed back into the graph).
- ``samples``     ingested *human* text, unique by sha256, with provenance.
- ``ingest_runs`` one row per ingestion run of a source.
- ``draft_edits`` text the user sent right after a Polytone inject (typing capture with
                  ``after_inject=True``), linked to the draft it edits. The text is NEVER learned
                  from; only its differences from the draft are (``corrections``).
- ``corrections`` per-persona edit learning (``backend/learning/edits.py``): one smoothed
                  delta per (feature, key). Word keys are blind indexes, the word is ciphertext.
- ``constraint_snapshots`` learning evaluations (``backend/learning/evaluate.py``): the constraints
                  generation would use for a persona at that time (graph + edit corrections; they hold
                  the user's words, so ``constraints_json`` is ciphertext), the decision and the
                  per-candidate scores (``eval_json``: numbers and labels only, plain).
- ``learning_state`` per-persona override set by an evaluation: ``pinned`` (generation uses snapshot
                  ``snapshot_id`` instead of graph + corrections) or ``corrections_paused``. No row = normal.
- ``personas``    personas the user added, and their own wording of a built-in persona's tone
                  (``backend/personas.py``). The tone description is the user's words, so it is
                  encrypted like sample text; the name stays plain (it is plain in Neo4j too).

Encryption at rest (CONTRACTS.md → Security): every column holding the user's words —
``drafts.intent/text/constraints_json``, ``samples.text/provenance_json``, ``draft_edits.text``,
``corrections.text``, ``constraint_snapshots.constraints_json`` —
is stored as ``enc1:`` AES-256-GCM ciphertext (AAD = ``"<table>.<column>"``) and decrypted
transparently on read (memoized in ``security.crypto``). The ``sha256`` columns of ``drafts`` and
``samples`` keep their name but hold the blind index ``idx1:...`` of the stripped text
(``text_index``), which is what dedupe and the firewall compare. ``Database.init`` migrates any
plaintext rows once (idempotent, one transaction per table, then VACUUM + WAL checkpoint); the
counts are in ``Database.migration`` (the server audits them as ``migration.encrypted``).
``secure_delete`` is on, so deleted rows (retention purge, wipe) are zeroed on disk.

Usage: ``Database(path)``; the process-wide instance lives on ``backend.runtime.Runtime.db``.

Thread-safety: each thread keeps one persistent connection (``threading.local``), so methods
can be called from any thread (FastAPI threadpool, background scorers) without paying a
connect per call. WAL mode + ``synchronous=NORMAL``: readers never block the writer and a
commit does not fsync (durable across app crashes; only an OS crash / power loss can drop
the last few commits). SELECTs run in autocommit, so every read sees the latest commit,
including writes from other processes (MCP servers).
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.security import crypto

log = logging.getLogger(__name__)

# Typing capture (source "typed") watches only these apps by default; opt-in, default OFF.
DEFAULT_CAPTURE_APPS: list[str] = [
    "com.tinyspeck.slackmacgap",
    "com.apple.MobileSMS",
    "com.apple.mail",
    "com.apple.Notes",
    "com.hnc.Discord",
    "net.whatsapp.WhatsApp",
    "ru.keepcoder.Telegram",
    "com.google.Chrome",
    "com.apple.Safari",
    "company.thebrowser.Browser",
    "notion.id",
]

DEFAULT_CONFIG: dict[str, str] = {
    "active_persona": "Default",
    "hotkey": "CmdOrCtrl+Shift+P",
    "inject_mode": "paste",
    "capture_enabled": "false",
    "capture_apps": json.dumps(DEFAULT_CAPTURE_APPS),
    "lock_idle_minutes": "15",
    # "quality" (POLYTONE_MODEL) | "fast" (llm.client.FAST_MODEL — lower latency, less literal)
    "draft_speed": "quality",
    "retention_days": "0",
    # days between automatic learning evaluations (backend/learning/evaluate.py); 0 = only on demand
    "learning_eval_days": "7",
}

# AAD contexts of the encrypted columns, and the blind-index context shared by drafts and
# samples (the firewall compares a sample's index with every draft's).
ENC_COLUMNS: dict[str, tuple[str, ...]] = {
    "drafts": ("intent", "text", "constraints_json"),
    "samples": ("text", "provenance_json"),
    "draft_edits": ("text",),
    "corrections": ("text",),
    "constraint_snapshots": ("constraints_json",),
}
CORRECTION_WORD_CONTEXT = "corrections.key"
CORRECTION_ALPHA = 0.2  # smoothed-mean weight of a new edit once 1/n drops below it
MAX_CORRECTION_WORDS = 500  # word rows kept per persona (most-seen first)
# what happened to a draft after it was shown (``drafts.outcome``). An outcome never replaces one
# of higher rank, so a late "accepted" (the inject) cannot undo "edited" (the send)
DRAFT_OUTCOMES = ("accepted", "regenerated", "discarded", "edited")
_OUTCOME_RANK = {"accepted": 0, "regenerated": 1, "discarded": 1, "edited": 2}
LEARNING_MODES = ("pinned", "corrections_paused")
TEXT_INDEX_CONTEXT = "content.text"
# sources whose samples the retention purge removes (CONTRACTS.md → Retention and files)
RETENTION_SOURCES = ("typed", "wispr")

# Columns added after their table first shipped. ``CREATE TABLE IF NOT EXISTS`` never alters an
# existing table, so ``Database.init`` adds the ones an older file lacks: (table, column, type).
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("drafts", "outcome", "TEXT"),
)

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
    sha256 TEXT NOT NULL,
    outcome TEXT
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
DROP INDEX IF EXISTS idx_samples_persona;
CREATE INDEX IF NOT EXISTS idx_samples_persona_created ON samples(persona, created_at, id);
CREATE INDEX IF NOT EXISTS idx_samples_created ON samples(created_at, id);
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
DROP INDEX IF EXISTS idx_runs_source;
CREATE INDEX IF NOT EXISTS idx_runs_source_started ON ingest_runs(source, started_at);
CREATE TABLE IF NOT EXISTS draft_edits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    app TEXT NOT NULL,
    text TEXT NOT NULL,
    nearest_draft_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_draft_edits_created ON draft_edits(created_at);
CREATE TABLE IF NOT EXISTS corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    persona TEXT NOT NULL COLLATE NOCASE,
    feature TEXT NOT NULL,
    key TEXT NOT NULL,
    text TEXT,
    value REAL NOT NULL,
    n INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(persona, feature, key)
);
CREATE TABLE IF NOT EXISTS constraint_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    persona TEXT NOT NULL COLLATE NOCASE,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    constraints_json TEXT,
    score REAL,
    eval_json TEXT,
    status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_persona ON constraint_snapshots(persona, id);
CREATE TABLE IF NOT EXISTS learning_state (
    persona TEXT PRIMARY KEY COLLATE NOCASE,
    mode TEXT NOT NULL,
    snapshot_id INTEGER,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS personas (
    name TEXT PRIMARY KEY COLLATE NOCASE,
    description TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def text_index(text: str) -> str:
    """Blind index (``idx1:``) of the stripped text — the dedupe key of drafts AND samples."""
    return crypto.blind_index(text.strip(), TEXT_INDEX_CONTEXT)


sha256_text = text_index  # historical name (the column is still called ``sha256``)

_legacy_warned: set[str] = set()


def _dec(value: str | None, ctx: str) -> str | None:
    """Decrypt a column value. A legacy plaintext value (written by an older build after this
    process migrated) is returned as is — the next start's migration encrypts it; an ``enc1:``
    value that fails to decrypt raises ``crypto.DecryptError``."""
    if value is None:
        return None
    if crypto.is_encrypted(value):
        return crypto.decrypt(value, ctx)
    if ctx not in _legacy_warned:
        _legacy_warned.add(ctx)
        log.warning("plaintext value in %s (encrypted at the next start)", ctx)
    return value


def _loads_enc(s: str | None, ctx: str) -> Any:
    return _loads(_dec(s, ctx))


def _config_value(v: Any) -> str:
    """bools/lists/dicts are stored as JSON so they round-trip; everything else as str."""
    if isinstance(v, (bool, list, tuple, dict)):
        return json.dumps(list(v) if isinstance(v, tuple) else v)
    return str(v)


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
        self._local = threading.local()
        self._initialized = False
        self.migration: dict[str, int] = {}
        # bumped whenever draft outcomes may have changed in this process: part of the few-shot
        # boost cache key (``langgraph/examples.py``)
        self.outcome_version = 0
        self.init()

    # ------------------------------------------------------------------ plumbing
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA secure_delete=ON")  # deleted rows are zeroed, not left in free pages
            self._local.conn = conn
        return conn

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """This thread's connection, inside a transaction (commit on success, rollback on error)."""
        conn = self._conn()
        with conn:
            yield conn

    def close(self) -> None:
        """Close the calling thread's connection (others close when their thread / self is gc'd)."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            self._local.conn = None
            conn.close()

    def init(self) -> None:
        with self._init_lock:
            if self._initialized:
                return
            with self.connect() as c:
                c.execute("PRAGMA journal_mode=WAL")
                c.executescript(_SCHEMA)
                self._ensure_columns(c)
                for k, v in DEFAULT_CONFIG.items():
                    c.execute("INSERT OR IGNORE INTO config(k, v) VALUES (?, ?)", (k, v))
            self._check_key()  # before migrating: never encrypt legacy rows with the wrong key
            self.migration = self._migrate_plaintext()
            self._initialized = True

    @staticmethod
    def _ensure_columns(c: sqlite3.Connection) -> None:
        """Add the ``_ADDED_COLUMNS`` an older database lacks. Idempotent (checks ``table_info``)."""
        for table, col, decl in _ADDED_COLUMNS:
            have = {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
            if col not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                log.info("schema: added %s.%s", table, col)

    # ------------------------------------------------------------------ encryption migration
    def _check_key(self) -> None:
        """Refuse to open a database whose rows were encrypted with another data key (Keychain
        item lost or replaced, wrong ``POLYTONE_DATA_KEY``). Without this the backend starts,
        every read of old rows fails, and new rows are written under the new key — a database
        no single key can read. Probes the newest ``enc1:`` value of each table."""
        conn = self._conn()
        for table, cols in ENC_COLUMNS.items():
            col = cols[0]
            row = conn.execute(
                f"SELECT {col} FROM {table} WHERE substr({col}, 1, 5) = 'enc1:' ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
            if row is None:
                continue
            try:
                crypto.decrypt(row[0], f"{table}.{col}")
            except crypto.DecryptError as e:
                raise crypto.KeyMismatch(
                    f"{self.path.name}: {table}.{col} does not decrypt with the current data key "
                    "(was the Keychain item com.polytone.app/data-key-v1 replaced, or POLYTONE_DATA_KEY "
                    "changed?). Refusing to start so no data is written under the wrong key."
                ) from e

    def _migrate_plaintext(self) -> dict[str, int]:
        """Encrypt every plaintext user-text value and turn plain sha256 hashes into blind
        indexes. Idempotent: ``enc1:``/``idx1:`` values are skipped, so a run that finds nothing
        costs one scan per table. One transaction per table; VACUUM + WAL checkpoint afterwards
        so no plaintext page lingers in the file or the WAL. Returns ``{table: rows changed}``."""
        conn = self._conn()
        counts: dict[str, int] = {}
        for table, cols in ENC_COLUMNS.items():
            key = "draft_id" if table == "drafts" else "id"
            has_idx = table in ("drafts", "samples")
            sel = ", ".join((key, *cols, *(("sha256",) if has_idx else ())))
            cond = " OR ".join(
                [f"({c} IS NOT NULL AND substr({c}, 1, 5) != 'enc1:')" for c in cols]
                + (["substr(sha256, 1, 5) != 'idx1:'"] if has_idx else [])
            )
            changed = dupes = 0
            with conn:  # one transaction per table: all of its rows or none
                rows = conn.execute(f"SELECT {sel} FROM {table} WHERE {cond}").fetchall()
                for r in rows:
                    sets: dict[str, Any] = {}
                    for c in cols:
                        v = r[c]
                        if v is not None and not crypto.is_encrypted(v):
                            sets[c] = crypto.encrypt(v, f"{table}.{c}")
                    if has_idx and not crypto.is_index(r["sha256"]):
                        sets["sha256"] = text_index(_dec(r["text"], f"{table}.text") or "")
                    assign = ", ".join(f"{c} = :{c}" for c in sets)
                    try:
                        conn.execute(
                            f"UPDATE {table} SET {assign} WHERE {key} = :_k",
                            {**sets, "_k": r[key]},
                        )
                    except sqlite3.IntegrityError:
                        # two legacy samples with different hashes but the same stripped text:
                        # keep the one already migrated (dedupe semantics), drop this duplicate
                        conn.execute(f"DELETE FROM {table} WHERE {key} = ?", (r[key],))
                        dupes += 1
                    changed += 1
            if changed:
                counts[table] = changed
            if dupes:
                counts[f"{table}_duplicates_dropped"] = dupes
        if counts:
            self.checkpoint(vacuum=True)
            log.info("encrypted plaintext rows at rest: %s", counts)
        return counts

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
                    (k, _config_value(v)),
                )
        return self.get_config()

    def capture_config(self) -> tuple[bool, list[str]]:
        """``(capture_enabled, capture_apps)`` parsed from the config table."""
        cfg = self.get_config()
        enabled = str(cfg.get("capture_enabled", "false")).strip().lower() in ("true", "1", "yes")
        apps = _loads(cfg.get("capture_apps"))
        if not isinstance(apps, list):
            apps = list(DEFAULT_CAPTURE_APPS)
        return enabled, [str(a) for a in apps if str(a).strip()]

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
            "constraints": constraints,
            "score": score,
            "sha256": text_index(text),
        }
        stored = {
            **row,
            "intent": crypto.encrypt(intent, "drafts.intent"),
            "text": crypto.encrypt(text, "drafts.text"),
            "constraints_json": crypto.encrypt(json.dumps(constraints), "drafts.constraints_json")
            if constraints is not None
            else None,
            "score_json": json.dumps(score) if score is not None else None,
        }
        with self.connect() as c:
            c.execute(
                "INSERT INTO drafts(draft_id, created_at, persona, intent, text, latency_ms,"
                " constraints_json, score_json, sha256) VALUES (:draft_id, :created_at, :persona,"
                " :intent, :text, :latency_ms, :constraints_json, :score_json, :sha256)",
                stored,
            )
        return dict(row)

    @staticmethod
    def _draft_out(row: Any) -> dict[str, Any]:
        r = dict(row)
        return {
            "draft_id": r["draft_id"],
            "created_at": r["created_at"],
            "persona": r["persona"],
            "intent": _dec(r["intent"], "drafts.intent"),
            "text": _dec(r["text"], "drafts.text"),
            "latency_ms": r["latency_ms"],
            "constraints": _loads_enc(r.get("constraints_json"), "drafts.constraints_json"),
            "score": _loads(r.get("score_json")),
            "sha256": r["sha256"],
            "outcome": r.get("outcome"),
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

    def set_draft_outcome(self, draft_id: str, outcome: str) -> str | None:
        """Record what happened to a draft (``DRAFT_OUTCOMES``). An outcome never replaces one of
        higher rank. Returns the outcome now stored, or None when the draft does not exist."""
        if outcome not in _OUTCOME_RANK:
            raise ValueError(f"unknown outcome {outcome!r}")
        with self.connect() as c:
            row = c.execute("SELECT outcome FROM drafts WHERE draft_id = ?", (draft_id,)).fetchone()
            if row is None:
                return None
            cur = row[0]
            if cur in _OUTCOME_RANK and _OUTCOME_RANK[cur] > _OUTCOME_RANK[outcome]:
                return cur
            c.execute("UPDATE drafts SET outcome = ? WHERE draft_id = ?", (outcome, draft_id))
        self.outcome_version += 1
        return outcome

    def kept_draft_texts(self, persona: str, limit: int = 30) -> list[str]:
        """What the user kept from ``persona``'s newest accepted / edited drafts: the draft text, or
        for an edited draft the message actually sent (its newest linked ``draft_edits`` row)."""
        with self.connect() as c:
            rows = c.execute(
                "SELECT d.outcome, d.text, (SELECT e.text FROM draft_edits e WHERE e.nearest_draft_id = d.draft_id"
                " ORDER BY e.id DESC LIMIT 1) FROM drafts d WHERE d.persona = ? AND d.outcome IN ('accepted', 'edited')"
                " ORDER BY d.created_at DESC, d.rowid DESC LIMIT ?",
                (persona, int(limit)),
            ).fetchall()
        out = []
        for outcome, text, sent in rows:
            if outcome == "edited" and sent:
                out.append(_dec(sent, "draft_edits.text") or "")
            else:
                out.append(_dec(text, "drafts.text") or "")
        return [t for t in out if t]

    def recent_draft_id(self, within_seconds: float = 600) -> str | None:
        """Id of the newest draft created in the last ``within_seconds``, else None."""
        cutoff = datetime.fromtimestamp(datetime.now(UTC).timestamp() - within_seconds, UTC)
        with self.connect() as c:
            row = c.execute(
                "SELECT draft_id FROM drafts WHERE created_at >= ? ORDER BY created_at DESC, rowid DESC"
                " LIMIT 1",
                (cutoff.isoformat(timespec="milliseconds"),),
            ).fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------------ draft edits
    def insert_draft_edit(
        self, *, app: str, text: str, nearest_draft_id: str | None = None, created_at: str | None = None
    ) -> int:
        """Store text the user sent right after a Polytone inject. Never reaches samples/graph."""
        with self.connect() as c:
            cur = c.execute(
                "INSERT INTO draft_edits(created_at, app, text, nearest_draft_id) VALUES (?, ?, ?, ?)",
                (created_at or utcnow_iso(), app, crypto.encrypt(text, "draft_edits.text"), nearest_draft_id),
            )
            return int(cur.lastrowid)

    def list_draft_edits(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as c:
            rows = c.execute(
                "SELECT * FROM draft_edits ORDER BY created_at DESC, id DESC LIMIT ?", (int(limit),)
            ).fetchall()
        return [{**dict(r), "text": _dec(r["text"], "draft_edits.text")} for r in rows]

    def drafts_within(self, within_seconds: float = 600, limit: int = 20) -> list[dict[str, Any]]:
        """``{draft_id, persona, text}`` of drafts created in the last ``within_seconds``, newest first."""
        cutoff = datetime.fromtimestamp(datetime.now(UTC).timestamp() - within_seconds, UTC)
        with self.connect() as c:
            rows = c.execute(
                "SELECT draft_id, persona, text FROM drafts WHERE created_at >= ?"
                " ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (cutoff.isoformat(timespec="milliseconds"), int(limit)),
            ).fetchall()
        return [{"draft_id": r[0], "persona": r[1], "text": _dec(r[2], "drafts.text") or ""} for r in rows]

    # ------------------------------------------------------------------ corrections (edit learning)
    def update_corrections(self, persona: str, rows: list[tuple[str, str, str | None, float]]) -> None:
        """Fold one edit into ``persona``'s corrections. ``rows`` = (feature, key, text, x); a row with
        ``text`` (a word) is keyed by its blind index and stores the word encrypted. Each value is a
        smoothed mean: ``value += (x - value) * max(1/n, CORRECTION_ALPHA)``."""
        now = utcnow_iso()
        with self.connect() as c:
            for feature, key, text, x in rows:
                if text is not None:
                    key = crypto.blind_index(text, CORRECTION_WORD_CONTEXT)
                cur = c.execute(
                    "SELECT value, n FROM corrections WHERE persona = ? AND feature = ? AND key = ?",
                    (persona, feature, key),
                ).fetchone()
                if cur is None:
                    c.execute(
                        "INSERT INTO corrections(persona, feature, key, text, value, n, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, 1, ?)",
                        (persona, feature, key,
                         crypto.encrypt(text, "corrections.text") if text is not None else None, float(x), now),
                    )
                else:
                    n = int(cur["n"]) + 1
                    value = float(cur["value"]) + (float(x) - float(cur["value"])) * max(1.0 / n, CORRECTION_ALPHA)
                    c.execute(
                        "UPDATE corrections SET value = ?, n = ?, updated_at = ?"
                        " WHERE persona = ? AND feature = ? AND key = ?",
                        (value, n, now, persona, feature, key),
                    )
            c.execute(  # bound the word rows: keep the most-seen, then the most recent
                "DELETE FROM corrections WHERE persona = ? AND feature = 'word' AND id NOT IN ("
                " SELECT id FROM corrections WHERE persona = ? AND feature = 'word'"
                " ORDER BY n DESC, updated_at DESC, id DESC LIMIT ?)",
                (persona, persona, MAX_CORRECTION_WORDS),
            )

    def get_corrections(self, persona: str) -> list[dict[str, Any]]:
        """``{feature, key, text, value, n, updated_at}`` rows of ``persona`` (``text`` decrypted)."""
        with self.connect() as c:
            rows = c.execute(
                "SELECT feature, key, text, value, n, updated_at FROM corrections WHERE persona = ?", (persona,)
            ).fetchall()
        return [{**dict(r), "text": _dec(r["text"], "corrections.text")} for r in rows]

    def delete_corrections(self, persona: str) -> int:
        with self.connect() as c:
            return c.execute("DELETE FROM corrections WHERE persona = ?", (persona,)).rowcount

    # ------------------------------------------------------------------ learning evaluations
    def insert_snapshot(
        self,
        *,
        persona: str,
        kind: str,
        status: str,
        constraints: dict | None,
        score: float | None,
        evaluation: dict | None,
        created_at: str | None = None,
    ) -> int:
        """Store an evaluation. ``constraints`` is encrypted (it holds the user's words);
        ``evaluation`` must hold numbers and labels only (it is stored plain)."""
        with self.connect() as c:
            cur = c.execute(
                "INSERT INTO constraint_snapshots(persona, created_at, kind, constraints_json, score, eval_json, status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    persona, created_at or utcnow_iso(), kind,
                    crypto.encrypt(json.dumps(constraints), "constraint_snapshots.constraints_json")
                    if constraints is not None else None,
                    None if score is None else float(score),
                    json.dumps(evaluation) if evaluation is not None else None,
                    status,
                ),
            )
            return int(cur.lastrowid)

    @staticmethod
    def _snapshot_out(r: Any, with_constraints: bool) -> dict[str, Any]:
        out = {
            "id": r["id"], "persona": r["persona"], "created_at": r["created_at"], "kind": r["kind"],
            "score": r["score"], "evaluation": _loads(r["eval_json"]) or {}, "status": r["status"],
        }
        if with_constraints:
            out["constraints"] = _loads_enc(r["constraints_json"], "constraint_snapshots.constraints_json")
        return out

    def list_snapshots(
        self, persona: str | None = None, limit: int = 50, *, status: str | None = None, with_constraints: bool = False
    ) -> list[dict[str, Any]]:
        """Evaluations, newest first. Constraints are decrypted only with ``with_constraints``."""
        q, where, args = "SELECT * FROM constraint_snapshots", [], []
        if persona:
            where.append("persona = ?")
            args.append(persona)
        if status:
            where.append("status = ?")
            args.append(status)
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        with self.connect() as c:
            rows = c.execute(q, args).fetchall()
        return [self._snapshot_out(r, with_constraints) for r in rows]

    def get_snapshot(self, snapshot_id: int) -> dict[str, Any] | None:
        with self.connect() as c:
            row = c.execute("SELECT * FROM constraint_snapshots WHERE id = ?", (int(snapshot_id),)).fetchone()
        return self._snapshot_out(row, True) if row else None

    def get_learning_state(self, persona: str) -> dict[str, Any] | None:
        """``{persona, mode, snapshot_id, updated_at}`` of ``persona``'s override, else None."""
        with self.connect() as c:
            row = c.execute("SELECT * FROM learning_state WHERE persona = ?", (persona,)).fetchone()
        return dict(row) if row else None

    def set_learning_state(self, persona: str, mode: str | None, snapshot_id: int | None = None) -> None:
        """Set (one of ``LEARNING_MODES``) or clear (``mode=None``) ``persona``'s override."""
        with self.connect() as c:
            if mode is None:
                c.execute("DELETE FROM learning_state WHERE persona = ?", (persona,))
                return
            if mode not in LEARNING_MODES:
                raise ValueError(f"unknown learning mode {mode!r}")
            c.execute(
                "INSERT INTO learning_state(persona, mode, snapshot_id, updated_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(persona) DO UPDATE SET mode = excluded.mode, snapshot_id = excluded.snapshot_id,"
                " updated_at = excluded.updated_at",
                (persona, mode, snapshot_id, utcnow_iso()),
            )

    def delete_learning(self, persona: str) -> int:
        """Drop ``persona``'s evaluations and override (persona delete). Returns snapshots deleted."""
        with self.connect() as c:
            n = c.execute("DELETE FROM constraint_snapshots WHERE persona = ?", (persona,)).rowcount
            c.execute("DELETE FROM learning_state WHERE persona = ?", (persona,))
        return n

    def learning_watermark(self, persona: str) -> dict[str, int]:
        """``{"sample_max_id", "samples", "edits"}`` of ``persona``: an evaluation is only worth
        repeating when one of these moved (a new sample, a learned edit)."""
        with self.connect() as c:
            n, top = c.execute(
                "SELECT COUNT(*), COALESCE(MAX(id), 0) FROM samples WHERE persona = ?", (persona,)
            ).fetchone()
            e = c.execute(
                "SELECT n FROM corrections WHERE persona = ? AND feature = 'edits' AND key = ''", (persona,)
            ).fetchone()
        return {"sample_max_id": int(top), "samples": int(n), "edits": int(e[0]) if e else 0}

    def all_draft_texts(self) -> list[str]:
        with self.connect() as c:
            rows = c.execute("SELECT text FROM drafts ORDER BY rowid").fetchall()
        return [_dec(r[0], "drafts.text") for r in rows]

    def draft_watermark(self) -> tuple[int, int, str]:
        """``(row count, max rowid, draft_id at max rowid)`` of ``drafts`` — changes whenever a
        draft is added/removed. The id is needed because SQLite reuses the rowids of deleted rows:
        after a wipe the next draft gets rowid 1 again, so count and max rowid alone can repeat."""
        with self.connect() as c:
            n, top, top_id = c.execute(
                "SELECT COUNT(*), COALESCE(MAX(rowid), 0),"
                " (SELECT draft_id FROM drafts ORDER BY rowid DESC LIMIT 1) FROM drafts"
            ).fetchone()
        return int(n), int(top), top_id or ""

    def drafts_since(self, rowid: int = 0) -> list[tuple[int, str, str, str]]:
        """``(rowid, sha256, text, draft_id)`` of drafts with ``rowid > rowid``, oldest first."""
        with self.connect() as c:
            rows = c.execute(
                "SELECT rowid, sha256, text, draft_id FROM drafts WHERE rowid > ? ORDER BY rowid", (int(rowid),)
            ).fetchall()
        return [(int(r[0]), r[1], _dec(r[2], "drafts.text"), r[3]) for r in rows]

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
                    crypto.encrypt(text, "samples.text"),
                    text_index(text),
                    crypto.encrypt(json.dumps(provenance or {}), "samples.provenance_json"),
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
                "text": _dec(r["text"], "samples.text"),
                "sha256": r["sha256"],
                "provenance": _loads_enc(r["provenance_json"], "samples.provenance_json") or {},
            }
            for r in rows
        ]

    def samples_watermark(self) -> tuple[int, int]:
        """``(row count, max id)`` of ``samples`` — changes whenever a sample is added/removed."""
        with self.connect() as c:
            n, top = c.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM samples").fetchone()
        return int(n), int(top)

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

    def count_samples_by_persona(self) -> dict[str, int]:
        with self.connect() as c:
            return {r[0]: int(r[1]) for r in c.execute("SELECT persona, COUNT(*) FROM samples GROUP BY persona")}

    def move_samples(self, src: str, dst: str) -> int:
        """Re-attribute every sample of persona ``src`` to ``dst``. Returns the rows moved."""
        with self.connect() as c:
            return c.execute("UPDATE samples SET persona = ? WHERE persona = ?", (dst, src)).rowcount

    # ------------------------------------------------------------------ personas
    def list_persona_rows(self) -> list[dict[str, str]]:
        """Stored persona rows (custom personas and built-in tone overrides), oldest first. A
        description that no longer decrypts (data key replaced) reads as empty."""
        with self.connect() as c:
            rows = c.execute("SELECT name, description, created_at FROM personas ORDER BY created_at, name").fetchall()
        out = []
        for r in rows:
            try:
                desc = _dec(r["description"], "personas.description") or ""
            except crypto.DecryptError:
                log.warning("persona description does not decrypt; treating it as empty")
                desc = ""
            out.append({"name": r["name"], "description": desc, "created_at": r["created_at"]})
        return out

    def upsert_persona_row(self, name: str, description: str) -> None:
        now = utcnow_iso()
        with self.connect() as c:
            c.execute(
                "INSERT INTO personas(name, description, created_at, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET description = excluded.description, updated_at = excluded.updated_at",
                (name, crypto.encrypt(description, "personas.description"), now, now),
            )

    def delete_persona_row(self, name: str) -> bool:
        with self.connect() as c:
            return c.execute("DELETE FROM personas WHERE name = ?", (name,)).rowcount > 0

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

    # ------------------------------------------------------------------ retention / wipe
    def purge_older_than(self, cutoff_iso: str) -> dict[str, int]:
        """Delete ``typed``/``wispr`` samples, ``draft_edits`` and ``drafts`` created before
        ``cutoff_iso`` (retention). Returns the rows deleted per table."""
        marks = ",".join("?" * len(RETENTION_SOURCES))
        with self.connect() as c:
            out = {
                "samples": c.execute(
                    f"DELETE FROM samples WHERE created_at < ? AND source IN ({marks})",
                    (cutoff_iso, *RETENTION_SOURCES),
                ).rowcount,
                "draft_edits": c.execute("DELETE FROM draft_edits WHERE created_at < ?", (cutoff_iso,)).rowcount,
                "drafts": c.execute("DELETE FROM drafts WHERE created_at < ?", (cutoff_iso,)).rowcount,
            }
        if out["drafts"]:
            self.outcome_version += 1
        if any(out.values()):
            self.checkpoint()
        return out

    def wipe_user_data(self) -> dict[str, int]:
        """Delete every sample, draft, draft edit, edit correction and learning evaluation (snapshots
        and overrides) (``POST /data/wipe``), then VACUUM."""
        with self.connect() as c:
            out = {t: c.execute(f"DELETE FROM {t}").rowcount
                   for t in ("samples", "drafts", "draft_edits", "corrections", "constraint_snapshots")}
            c.execute("DELETE FROM learning_state")
        self.outcome_version += 1
        self.checkpoint(vacuum=True)
        return out

    def checkpoint(self, vacuum: bool = False) -> None:
        """Optionally VACUUM (rebuild the file without free pages), then truncate the WAL."""
        conn = self._conn()
        if vacuum:
            conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
