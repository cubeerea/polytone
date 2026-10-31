"""Tamper-evident audit log (``<data_dir>/audit.sqlite3``). See CONTRACTS.md → Security.

Each row: ``seq, ts, event, actor, detail (canonical JSON), prev, mac`` where
``mac = HMAC-SHA256(subkey("audit"), prev || canonical-JSON([seq, ts, event, actor, detail]))``
and ``prev`` is the previous row's mac (genesis: 64 × "0"). SQLite triggers abort UPDATE and
DELETE. The head (``seq``, ``mac``) is mirrored in ``<data_dir>/.audit_head`` (0600) so a
truncated tail is detected. ``verify()`` walks the chain and reports the first bad row.

Never put user text in ``detail``: lengths, counts, ids, persona names and app bundle ids only.
``append`` scrubs any string value longer than ``MAX_DETAIL_STR`` as a safety net.

Appends are serialized across processes (``fcntl.flock`` on ``.audit.lock`` + ``BEGIN IMMEDIATE``),
so the MCP server processes can log into the same chain.
"""
from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.security.keys import subkey

log = logging.getLogger(__name__)

GENESIS = "0" * 64
MAX_DETAIL_STR = 200
HEAD_FILE = ".audit_head"
LOCK_FILE = ".audit.lock"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    seq INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    event TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail TEXT NOT NULL,
    prev TEXT NOT NULL,
    mac TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit log is append-only'); END;
"""


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _scrub(v: Any) -> Any:
    if isinstance(v, str):
        return v if len(v) <= MAX_DETAIL_STR else f"<{len(v)} chars dropped>"
    if isinstance(v, dict):
        return {str(k): _scrub(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_scrub(x) for x in v]
    if v is None or isinstance(v, (bool, int, float)):
        return v
    return str(v)[:MAX_DETAIL_STR]


def compute_mac(prev: str, seq: int, ts: str, event: str, actor: str, detail: Any) -> str:
    msg = prev.encode("ascii") + canonical([seq, ts, event, actor, detail]).encode("utf-8")
    return hmac.new(subkey("audit"), msg, hashlib.sha256).hexdigest()


def _secure(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.head_path = self.path.parent / HEAD_FILE
        self.lock_path = self.path.parent / LOCK_FILE
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        for p in (self.path, self.lock_path):
            if not p.exists():
                p.touch()
            _secure(p)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _exclusive(self):
        with self._lock, open(self.lock_path, "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    # ------------------------------------------------------------------ write
    def append(self, event: str, detail: dict[str, Any] | None = None, actor: str = "backend") -> int:
        """Append one event; returns its seq. Never raises (the audit log must not break a request)."""
        try:
            return self._append(event, _scrub(dict(detail or {})), actor)
        except Exception as e:  # noqa: BLE001
            log.error("audit append failed for %s: %s", event, e.__class__.__name__)
            return -1

    def _append(self, event: str, detail: dict[str, Any], actor: str) -> int:
        ts = datetime.now(UTC).isoformat(timespec="milliseconds")
        with self._exclusive():
            c = self._conn
            c.execute("BEGIN IMMEDIATE")
            try:
                row = c.execute("SELECT seq, mac FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
                seq, prev = (row[0] + 1, row[1]) if row else (1, GENESIS)
                mac = compute_mac(prev, seq, ts, event, actor, detail)
                c.execute(
                    "INSERT INTO audit_log(seq, ts, event, actor, detail, prev, mac) VALUES (?,?,?,?,?,?,?)",
                    (seq, ts, event, actor, canonical(detail), prev, mac),
                )
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
            self._write_head(seq, mac)
        return seq

    def _write_head(self, seq: int, mac: str) -> None:
        tmp = self.head_path.with_name(self.head_path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(canonical({"seq": seq, "mac": mac}))
        os.replace(tmp, self.head_path)

    # ------------------------------------------------------------------ read
    def entries(self, limit: int = 100, before_seq: int | None = None) -> list[dict[str, Any]]:
        q = "SELECT seq, ts, event, actor, detail, prev, mac FROM audit_log"
        args: list[Any] = []
        if before_seq is not None:
            q += " WHERE seq < ?"
            args.append(int(before_seq))
        q += " ORDER BY seq DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [
            {"seq": r[0], "ts": r[1], "event": r[2], "actor": r[3], "detail": json.loads(r[4]), "prev": r[5],
             "mac": r[6]}
            for r in rows
        ]

    def head_seq(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COALESCE(MAX(seq), 0) FROM audit_log").fetchone()
        return int(row[0])

    def _read_head(self) -> dict[str, Any] | None:
        """``{"seq": int, "mac": str}``, None if the file is missing, seq -1 if it is unreadable
        or malformed (verify must report that, not crash)."""
        try:
            head = json.loads(self.head_path.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return {"seq": -1, "mac": ""}
        seq = head.get("seq") if isinstance(head, dict) else None
        if not isinstance(seq, int) or isinstance(seq, bool):
            return {"seq": -1, "mac": ""}
        return {"seq": seq, "mac": str(head.get("mac", ""))}

    def verify(self) -> dict[str, Any]:
        """Walk the chain: ``{"ok", "count", "first_bad_seq", "reason"}``."""
        def bad(seq: int | None, reason: str, count: int) -> dict[str, Any]:
            return {"ok": False, "count": count, "first_bad_seq": seq, "reason": reason}

        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, ts, event, actor, detail, prev, mac FROM audit_log ORDER BY seq"
            ).fetchall()
        prev, expect, count = GENESIS, 1, 0
        macs: dict[int, str] = {}
        for seq, ts, event, actor, detail, row_prev, mac in rows:
            if seq != expect:
                return bad(expect, f"missing rows (expected seq {expect}, found {seq})", count)
            if row_prev != prev:
                return bad(seq, "chain broken (prev does not match the previous mac)", count)
            try:
                d = json.loads(detail)
            except ValueError:
                return bad(seq, "detail is not valid JSON", count)
            if not hmac.compare_digest(compute_mac(prev, seq, ts, event, actor, d), mac):
                return bad(seq, "mac mismatch (row altered, or the audit key changed)", count)
            macs[seq] = mac
            prev, expect, count = mac, expect + 1, count + 1
        head = self._read_head()
        last = expect - 1
        if head is None:
            if last:
                return bad(last + 1, f"{HEAD_FILE} is missing", count)
        elif head["seq"] < 0:
            return bad(None, f"{HEAD_FILE} is unreadable", count)
        elif head["seq"] > last:
            return bad(last + 1, f"truncated: {HEAD_FILE} records seq {head['seq']}, log ends at {last}", count)
        elif head["seq"] and not hmac.compare_digest(head["mac"].encode(), macs[head["seq"]].encode()):
            # head == last, or head < last (a crash between commit and head write; the extra
            # rows verified above): either way the head must name a real row
            return bad(head["seq"], f"{HEAD_FILE} mac does not match row {head['seq']}", count)
        return {"ok": True, "count": count, "first_bad_seq": None, "reason": None}
