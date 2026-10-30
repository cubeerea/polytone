"""Drop-folder source + MCP server: watches ``POLYTONE_WATCH_DIR`` (default ``data/inbox``).

Supported files
- ``.txt`` / ``.md`` — the whole file is one sample; files longer than ``TXT_SPLIT_CHARS`` are
  split on blank lines (one sample per paragraph).
- ``.mbox`` — stdlib ``mailbox``; ONLY messages whose ``From`` address is one of the user's
  addresses (SQLite config ``user_emails`` / env ``POLYTONE_USER_EMAILS`` / the authenticated
  Gmail address) are kept. Inbound mail is skipped. Quotes/signatures are stripped.
- SQLite messaging dumps (``.db``/``.sqlite``/``.sqlite3`` or any file with the SQLite header):
  * iMessage ``chat.db`` shape: ``message.text`` WHERE ``message.is_from_me = 1``;
  * generic: a table with a text column (text/body/message/content) plus either an
    ``is_from_me``-style flag (rows = 1) or a sender column (rows whose sender is one of the
    user's addresses or ``me``/``self``);
  * anything else is skipped with a reason.
  Dumps are opened ``mode=ro``.

Files are tracked by sha256 (table ``watcher_files`` in the Polytone SQLite DB), so rescans are
idempotent; a changed file has a new hash and is re-read (samples are deduped by text anyway).
Every item goes through ``run_ingest`` (firewall) — nothing is written to the graph directly.

``collect(settings)`` = one-shot scan (daemon ``/ingest/sources/watcher/run``);
``watch(...)`` / ``WatcherThread`` = continuous watchdog observer feeding ``run_ingest``.
The daemon starts a ``WatcherThread`` at startup when the directory exists and
``POLYTONE_WATCHER`` is not ``0``.
"""
from __future__ import annotations

import hashlib
import logging
import mailbox
import re
import sqlite3
import sys
import threading
import time
from email.message import Message
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from backend.mcp_servers._common import (
    SourceItem,
    address_of,
    clean_email_body,
    html_to_text,
    make_server,
    outbound,
    state_db,
    user_emails,
)

log = logging.getLogger(__name__)

TXT_SUFFIXES = {".txt", ".md", ".text"}
MBOX_SUFFIXES = {".mbox", ".mbx"}
SQLITE_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".chatdb"}
TXT_SPLIT_CHARS = 4000
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_ROWS_PER_DUMP = 50_000
TEXT_COLS = ("text", "body", "message", "content", "message_text", "msg")
FROM_ME_COLS = ("is_from_me", "from_me", "isfromme", "is_outgoing", "outgoing", "is_sent", "sent_by_me")
SENDER_COLS = ("sender", "from", "from_addr", "sender_email", "author", "from_email", "sender_id")
SELF_SENDERS = {"me", "self", "myself"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS watcher_files (
    sha256 TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    processed_at TEXT NOT NULL,
    items INTEGER NOT NULL DEFAULT 0,
    note TEXT
);
"""
_scan_lock = threading.Lock()


class Skip(Exception):
    pass


# --------------------------------------------------------------------------- tracking
def _ensure_table(db: Any) -> None:
    with db.connect() as c:
        c.executescript(_SCHEMA)


def _processed(db: Any, sha: str) -> bool:
    with db.connect() as c:
        return c.execute("SELECT 1 FROM watcher_files WHERE sha256 = ?", (sha,)).fetchone() is not None


def _mark(db: Any, sha: str, path: Path, items: int, note: str | None = None) -> None:
    from backend.storage.db import utcnow_iso

    with db.connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO watcher_files(sha256, path, processed_at, items, note) VALUES (?,?,?,?,?)",
            (sha, str(path), utcnow_iso(), items, note),
        )


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def watch_dir(settings: Any, create: bool = True) -> Path:
    d = Path(settings.polytone_watch_dir).expanduser()
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


# --------------------------------------------------------------------------- parsers
def parse_txt(path: Path) -> list[SourceItem]:
    text = path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n").strip()
    if not text:
        return []
    chunks = [text] if len(text) <= TXT_SPLIT_CHARS else [p.strip() for p in re.split(r"\n\s*\n", text)]
    return [
        {"text": c, "provenance": outbound("watcher", kind="txt", file=path.name, part=i)}
        for i, c in enumerate(chunks) if c
    ]


def _mail_body(msg: Message) -> str:
    plain, htm = [], []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.is_multipart() or part.get_filename() or part.get_content_disposition() == "attachment":
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        (plain if ctype == "text/plain" else htm).append(text)
    if any(p.strip() for p in plain):
        return "\n".join(plain)
    return "\n".join(html_to_text(h) for h in htm)


def parse_mbox(path: Path, mine: set[str]) -> tuple[list[SourceItem], dict[str, int]]:
    if not mine:
        raise Skip("no user_emails configured (set POLYTONE_USER_EMAILS or config user_emails)")
    items: list[SourceItem] = []
    counts = {"messages": 0, "inbound_skipped": 0, "empty": 0}
    box = mailbox.mbox(str(path), create=False)
    try:
        for msg in box:
            counts["messages"] += 1
            if address_of(msg.get("From")) not in mine:
                counts["inbound_skipped"] += 1
                continue
            text = clean_email_body(_mail_body(msg))
            if not text:
                counts["empty"] += 1
                continue
            date = msg.get("Date")
            try:
                date = parsedate_to_datetime(date).isoformat() if date else None
            except (TypeError, ValueError):
                pass
            items.append({
                "text": text,
                "provenance": outbound("watcher", kind="mbox", file=path.name, from_me=True,
                                       message_id=msg.get("Message-ID"), date=date),
            })
    finally:
        box.close()
    return items, counts


def is_sqlite(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return fh.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def parse_sqlite(path: Path, mine: set[str]) -> tuple[list[SourceItem], dict[str, Any]]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tables = {r[0]: [c[1] for c in conn.execute(f"PRAGMA table_info({_q(r[0])})")]
                  for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        lower_tables = {t.lower(): t for t in tables}
        # --- iMessage chat.db
        if "message" in lower_tables:
            t = lower_tables["message"]
            cols = {c.lower(): c for c in tables[t]}
            if "text" in cols and "is_from_me" in cols:
                rows = conn.execute(
                    f"SELECT {_q(cols['text'])}{', ' + _q(cols['date']) if 'date' in cols else ''} FROM {_q(t)} "
                    f"WHERE {_q(cols['is_from_me'])} = 1 AND {_q(cols['text'])} IS NOT NULL "
                    f"AND TRIM({_q(cols['text'])}) != '' LIMIT {MAX_ROWS_PER_DUMP}"
                ).fetchall()
                return [
                    {"text": r[0].strip(), "provenance": outbound("watcher", kind="imessage", file=path.name,
                                                                  from_me=True, date=r[1] if len(r) > 1 else None)}
                    for r in rows if isinstance(r[0], str)
                ], {"shape": "imessage", "table": t}
        # --- generic messaging table
        reasons = []
        for t, colnames in tables.items():
            cols = {c.lower(): c for c in colnames}
            text_col = next((cols[c] for c in TEXT_COLS if c in cols), None)
            if not text_col:
                continue
            flag = next((cols[c] for c in FROM_ME_COLS if c in cols), None)
            sender = next((cols[c] for c in SENDER_COLS if c in cols), None)
            if flag:
                rows = conn.execute(
                    f"SELECT {_q(text_col)} FROM {_q(t)} WHERE {_q(flag)} IN (1, '1', 'true', 'TRUE') "
                    f"AND {_q(text_col)} IS NOT NULL LIMIT {MAX_ROWS_PER_DUMP}"
                ).fetchall()
                texts = [r[0] for r in rows]
            elif sender:
                rows = conn.execute(
                    f"SELECT {_q(text_col)}, {_q(sender)} FROM {_q(t)} WHERE {_q(text_col)} IS NOT NULL "
                    f"LIMIT {MAX_ROWS_PER_DUMP * 4}"
                ).fetchall()
                ok = mine | SELF_SENDERS
                texts = [r[0] for r in rows if (address_of(str(r[1])) or str(r[1]).strip().lower()) in ok
                         or str(r[1]).strip().lower() in ok]
            else:
                reasons.append(f"{t}: text column but no is_from_me/sender column")
                continue
            items = [
                {"text": s.strip(), "provenance": outbound("watcher", kind="sqlite", file=path.name, table=t,
                                                           from_me=True)}
                for s in texts if isinstance(s, str) and s.strip()
            ]
            return items, {"shape": "generic", "table": t, "text_col": text_col, "by": flag or sender}
        raise Skip("unrecognised SQLite dump (" + ("; ".join(reasons) or "no text column") + ")")
    finally:
        conn.close()


def parse_file(path: Path, mine: set[str]) -> tuple[list[SourceItem], str]:
    """Items from one file + a short note. Raises ``Skip`` with a reason."""
    suffix = path.suffix.lower()
    if path.stat().st_size > MAX_FILE_BYTES:
        raise Skip("file too large")
    if suffix in TXT_SUFFIXES:
        items = parse_txt(path)
        return items, f"txt: {len(items)} sample(s)"
    if suffix in MBOX_SUFFIXES:
        items, counts = parse_mbox(path, mine)
        return items, f"mbox: kept {len(items)}/{counts['messages']}, inbound skipped {counts['inbound_skipped']}"
    if suffix in SQLITE_SUFFIXES or is_sqlite(path):
        if not is_sqlite(path):
            raise Skip("not an SQLite database")
        items, info = parse_sqlite(path, mine)
        return items, f"sqlite[{info['shape']}:{info['table']}]: {len(items)} message(s)"
    raise Skip("unsupported file type")


# --------------------------------------------------------------------------- scan
def _candidates(d: Path) -> list[Path]:
    out = []
    for p in sorted(d.rglob("*")):
        if not p.is_file() or p.name.startswith(".") or p.name.endswith(("-wal", "-shm", "-journal")):
            continue
        out.append(p)
    return out


def scan(settings: Any, db: Any = None) -> tuple[list[SourceItem], list[dict[str, Any]]]:
    """One-shot scan of the watch dir: ``(items, report)``. Marks parsed files as processed."""
    db = db or state_db(settings)
    _ensure_table(db)
    d = watch_dir(settings)
    mine = user_emails(settings, db)
    items: list[SourceItem] = []
    report: list[dict[str, Any]] = []
    with _scan_lock:
        for path in _candidates(d):
            rel = str(path.relative_to(d))
            try:
                sha = file_sha256(path)
            except OSError as e:
                report.append({"file": rel, "status": "error", "reason": str(e)})
                continue
            if _processed(db, sha):
                report.append({"file": rel, "status": "already processed"})
                continue
            try:
                got, note = parse_file(path, mine)
            except Skip as e:
                report.append({"file": rel, "status": "skipped", "reason": str(e)})
                continue
            except (OSError, sqlite3.Error, UnicodeError, ValueError) as e:
                report.append({"file": rel, "status": "error", "reason": f"{e.__class__.__name__}: {e}"})
                continue
            for it in got:
                it["provenance"]["file_sha256"] = sha
            items.extend(got)
            _mark(db, sha, path, len(got), note)
            report.append({"file": rel, "status": "parsed", "items": len(got), "note": note})
    return items, report


# --------------------------------------------------------------------------- source hook
def is_enabled(settings: Any) -> bool:
    return True


def status(settings: Any) -> str:
    d = watch_dir(settings, create=False)
    if not d.is_dir():
        return f"ready (watch dir {d} will be created on first run)"
    db = state_db(settings)
    _ensure_table(db)
    with db.connect() as c:
        n = c.execute("SELECT COUNT(*) FROM watcher_files").fetchone()[0]
    mine = user_emails(settings, db)
    hint = "" if mine else "; set POLYTONE_USER_EMAILS to import .mbox"
    running = " watching" if _active_thread is not None and _active_thread.is_alive() else ""
    return f"ready{running} ({d}, {n} file(s) processed{hint})"


def collect(settings: Any) -> list[SourceItem]:
    items, report = scan(settings)
    for r in report:
        if r["status"] in ("skipped", "error"):
            log.info("watcher: %s %s: %s", r["status"], r["file"], r.get("reason"))
    return items


def scan_and_ingest(runtime: Any = None) -> dict[str, Any]:
    from backend.langgraph.ingest_graph import run_ingest_batch
    from backend.runtime import get_runtime

    rt = runtime or get_runtime()
    items, report = scan(rt.settings, rt.db)
    res = run_ingest_batch(items, "watcher", runtime=rt) if items else {
        "accepted": 0, "rejected": 0, "reasons": [], "sentences": 0, "words_merged": 0}
    return {**res, "files": report}


# --------------------------------------------------------------------------- continuous watch
class WatcherThread(threading.Thread):
    """watchdog observer on the watch dir; debounces events then scans + ingests."""

    def __init__(self, runtime: Any, debounce: float = 1.0):
        super().__init__(name="polytone-watcher", daemon=True)
        self.rt = runtime
        self.debounce = debounce
        self._stop_evt = threading.Event()
        self._dirty = threading.Event()
        self.runs = 0
        self.last_result: dict[str, Any] | None = None

    def run(self) -> None:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        thread = self

        class Handler(FileSystemEventHandler):
            def on_any_event(self, event: Any) -> None:
                if not event.is_directory and event.event_type in ("created", "modified", "moved", "closed"):
                    thread._dirty.set()

        d = watch_dir(self.rt.settings)
        observer = Observer()
        observer.schedule(Handler(), str(d), recursive=True)
        observer.start()
        self._dirty.set()  # initial scan
        try:
            while not self._stop_evt.is_set():
                if not self._dirty.wait(timeout=0.5):
                    continue
                # debounce: wait until events stop arriving (files still being written)
                while self._dirty.is_set() and not self._stop_evt.is_set():
                    self._dirty.clear()
                    self._stop_evt.wait(self.debounce)
                if self._stop_evt.is_set():
                    break
                try:
                    self.last_result = scan_and_ingest(self.rt)
                    self.runs += 1
                    if self.last_result["accepted"] or self.last_result["rejected"]:
                        log.info("watcher ingested: accepted=%s rejected=%s",
                                 self.last_result["accepted"], self.last_result["rejected"])
                except Exception:
                    log.exception("watcher scan failed")
        finally:
            observer.stop()
            observer.join(timeout=5)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_evt.set()
        self._dirty.set()
        if self.is_alive():
            self.join(timeout)


_active_thread: WatcherThread | None = None


def start_watcher(runtime: Any) -> WatcherThread:
    global _active_thread
    t = WatcherThread(runtime)
    t.start()
    _active_thread = t
    return t


def stop_watcher(t: WatcherThread | None = None) -> None:
    global _active_thread
    t = t or _active_thread
    if t is not None:
        t.stop()
    if t is _active_thread:
        _active_thread = None


def watch(runtime: Any = None) -> None:
    """Run the watcher in the foreground until Ctrl+C."""
    from backend.runtime import get_runtime

    t = start_watcher(runtime or get_runtime())
    try:
        while t.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop_watcher(t)


# --------------------------------------------------------------------------- MCP server
mcp = make_server(
    "polytone-file-watcher",
    "Imports the user's own writing from the Polytone drop folder (.txt, .mbox from the user, SQLite chat dumps).",
)


@mcp.tool()
def scan_now() -> dict[str, Any]:
    """Scan the watch folder once and ingest new files through the firewall."""
    return scan_and_ingest()


@mcp.tool(name="status")
def status_tool() -> dict[str, Any]:
    """Watch folder, processed files and configured user addresses."""
    from backend.runtime import get_runtime

    rt = get_runtime()
    db = rt.db
    _ensure_table(db)
    with db.connect() as c:
        files = [dict(r) for r in c.execute(
            "SELECT path, processed_at, items, note FROM watcher_files ORDER BY processed_at DESC LIMIT 50")]
    return {
        "status": status(rt.settings), "watch_dir": str(watch_dir(rt.settings, create=False)),
        "user_emails": sorted(user_emails(rt.settings, db)), "processed_files": files,
        "items_ingested": db.count_samples(source="watcher"),
    }


def main(argv: list[str] | None = None) -> None:
    import json

    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "serve"
    if cmd == "serve":
        mcp.run("stdio")
    elif cmd == "watch":
        logging.basicConfig(level=logging.INFO)
        watch()
    elif cmd == "scan":
        print(json.dumps(scan_and_ingest(), indent=2, default=str))
    else:
        raise SystemExit("usage: python -m backend.mcp_servers.file_watcher [serve|watch|scan]")


if __name__ == "__main__":
    main()
