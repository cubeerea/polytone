"""Live Wispr Flow ingestion + the index of recent Wispr insertions used by typing capture.

Wispr Flow has no local push API, but it commits every dictation to its SQLite history
(``flow.sqlite``, WAL mode) about a second after the dictation ends. ``WisprLiveWatcher`` (a
daemon thread the API server starts at boot) therefore:

1. ``stat``s ``flow.sqlite`` and ``flow.sqlite-wal`` every ``poll_s`` (default 1s; ~µs);
2. on a change, waits until the pair is stable for ``debounce_s`` (300ms);
3. snapshots (APFS clone) + reads ``rowid > cursor`` via ``wispr.read_increment`` — the same
   cursor the on-demand ``POST /ingest/sources/wispr/run`` uses;
4. ingests each row's RAW ``asrText`` through ``run_ingest_batch`` (full firewall), source
   ``wispr``, persona ``Default``, provenance ``raw_transcript=True, kind="spoken"`` + app/url/
   transcript id/rowid; and
5. feeds every row's *inserted* text (pastedText / serverFinalizedText / formattedText) into
   ``INSERTIONS``. That text is never learned.

``INSERTIONS`` lets ``POST /ingest/typed`` tell Wispr output apart from typing: a message the user
sends right after dictating contains Wispr's AI-formatted text, which must not be learned as typed.
``check_typed`` refreshes the index from the DB itself when the DB changed since its last look
(cold start, watcher off, or the row committed after the watcher's last poll), waiting up to
``WISPR_TYPED_GRACE_MS`` (default 1500) for a dictation that is still being committed.

Env: ``POLYTONE_WISPR_LIVE`` (default on; only starts when a Wispr history DB exists —
``POLYTONE_WISPR_LOCAL=0`` turns both off), ``WISPR_TYPED_GRACE_MS``.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

from backend.ingestion.text import token_spans, tokenize
from backend.mcp_servers import wispr
from backend.mcp_servers._common import truthy_env

log = logging.getLogger(__name__)

POLL_S = 1.0
DEBOUNCE_S = 0.3
TICK_CAP = 200  # rows per read; a backlog (first run) is worked off over consecutive ticks
WINDOW_S = 30 * 60  # how long a Wispr insertion is remembered
COLD_ROWS = 50  # a cold index looks at the last 50 rows only
MAX_DICTATION_S = 15 * 60  # start→insert slack when filtering rows by (start) timestamp
COVERAGE = 0.8  # shingle coverage (either direction) that counts as "this is Wispr's text"
GAP_FILL = 2  # unmarked runs of ≤ 2 tokens between two Wispr spans are edits of it
DEFAULT_GRACE_MS = 1500


# =========================================================================== insertion index
@dataclass
class Insertion:
    rowid: int
    app: str | None
    at: float  # epoch seconds the text was (about to be) inserted: start + duration
    variants: tuple[tuple[str, ...], ...]  # token sequences, longest first


@dataclass
class StripResult:
    text: str  # the remainder after removing Wispr spans (== input when nothing matched)
    matched: list[int] = field(default_factory=list)  # Wispr rowids found in the message
    removed_tokens: int = 0


def _same_app(a: str | None, b: str | None) -> bool:
    return not a or not b or a.strip().lower() == b.strip().lower()


class InsertionIndex:
    """Recent Wispr insertions (last ``WINDOW_S``), keyed by History rowid."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._entries: OrderedDict[int, Insertion] = OrderedDict()
        self._sig: dict[str, tuple] = {}  # db path → file signature at the index's last own read
        self._top: dict[str, int] = {}  # db path → highest rowid the index has looked at

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._sig.clear()
            self._top.clear()

    def __len__(self) -> int:
        return len(self._entries)

    def add_rows(self, rows: list[wispr.HistoryRow], now: float | None = None) -> int:
        now = time.time() if now is None else now
        added = 0
        with self._lock:
            for r in rows:
                texts = list(r.inserted)
                if r.text:  # a raw-transcript paste (status raw_transcript) is Wispr output too
                    texts.append(r.text)
                variants = sorted({v for v in (tuple(tokenize(t)) for t in texts) if v}, key=len, reverse=True)
                if not variants:
                    continue
                ep = r.epoch
                at = ep + (r.duration or 0.0) if ep is not None else now
                if now - at > WINDOW_S:
                    continue
                self._entries[r.rowid] = Insertion(r.rowid, r.app, at, tuple(variants))
                self._entries.move_to_end(r.rowid)
                added += 1
            self._prune(now)
        return added

    def _prune(self, now: float) -> None:
        for rid in [rid for rid, e in self._entries.items() if now - e.at > WINDOW_S]:
            del self._entries[rid]

    def candidates(self, app: str | None, now: float | None = None) -> list[Insertion]:
        now = time.time() if now is None else now
        with self._lock:
            self._prune(now)
            return [e for e in reversed(self._entries.values()) if _same_app(e.app, app)]

    def note_rowid(self, db_path: Path, rowid: int) -> None:
        key = str(db_path)
        with self._lock:
            self._top[key] = max(self._top.get(key, 0), rowid)

    def refresh(self, db_path: Path) -> bool:
        """Read rows the index hasn't seen when the DB changed since its own last read (cheap
        no-op otherwise). Looks at most ``COLD_ROWS`` back from the newest row."""
        key = str(db_path)
        sig = wispr.file_signature(db_path)
        if self._sig.get(key) == sig:
            return False
        with self._refresh_lock:
            if self._sig.get(key) == sig:
                return False
            with wispr.open_snapshot(db_path) as conn:
                schema = wispr.inspect_schema(conn)
                if schema is None:
                    self._sig[key] = sig
                    return False
                top = wispr.max_rowid(conn, schema)
                after = max(self._top.get(key, 0), top - COLD_ROWS)
                # only rows that can still be in the window (timestamp = dictation START)
                newest = wispr.newest_ts(conn, schema) or ""
                since = wispr.wispr_ts(time.time() - WINDOW_S - MAX_DICTATION_S) if newest.endswith("+00:00") else None
                rows = (wispr.fetch_rows(conn, schema, after_rowid=after, cap=COLD_ROWS * 4, since_ts=since)
                        if top > after else [])
            self.add_rows(rows)
            self.note_rowid(db_path, max([top] + [r.rowid for r in rows]))
            self._sig[key] = sig
            return True


INSERTIONS = InsertionIndex()


def strip_insertions(text: str, insertions: list[Insertion]) -> StripResult:
    """Remove the spans of ``text`` that are Wispr insertions.

    A Wispr insertion is found in the message when its token sequence occurs in it verbatim
    (≥ 2 tokens, or the whole message), or — lightly edited — when ≥ ``COVERAGE`` of its 3-word
    shingles are in the message or ≥ ``COVERAGE`` of the message's shingles are in it. The tokens
    covered by shared shingles are removed (plus ≤ ``GAP_FILL``-token gaps between them, i.e.
    words the user corrected inside the dictation) with their trailing punctuation."""
    toks = [t for t in token_spans(text) if t.kind != "url"]
    seq = [t.text for t in toks]
    n = len(seq)
    if not n or not insertions:
        return StripResult(text)
    marked = [False] * n
    grams = [tuple(seq[i:i + 3]) for i in range(n - 2)]
    text_grams = set(grams)
    matched: list[int] = []
    for ins in insertions:
        hit = False
        for var in ins.variants:  # 1) verbatim, any variant (pasted / finalized / formatted / raw)
            m = len(var)
            if m >= 2 or m == n:
                first = var[0]
                for i in range(n - m + 1):
                    if seq[i] == first and tuple(seq[i:i + m]) == var:
                        marked[i:i + m] = [True] * m
                        hit = True
        if not hit and n >= 3:  # 2) edited: every variant that covers enough contributes its spans
            for var in ins.variants:
                m = len(var)
                if m < 3:
                    continue
                vg = {tuple(var[i:i + 3]) for i in range(m - 2)}
                common = vg & text_grams
                if len(common) >= COVERAGE * len(vg) or len(common) >= COVERAGE * len(text_grams):
                    for i, g in enumerate(grams):
                        if g in common:
                            marked[i:i + 3] = [True] * 3
                    hit = True
        if hit:
            matched.append(ins.rowid)
    if not matched:
        return StripResult(text)
    # fill short gaps between two marked runs (the user fixed a word inside the dictation)
    idx = [i for i, m in enumerate(marked) if m]
    for a, b in pairwise(idx):
        if 1 < b - a <= GAP_FILL + 1:
            marked[a:b] = [True] * (b - a)
    out: list[str] = []
    pos = 0
    i = 0
    while i < n:
        if not marked[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and marked[j + 1]:
            j += 1
        start, end = toks[i].start, toks[j].end
        while end < len(text) and not text[end].isspace() and not text[end].isalnum():
            end += 1  # trailing punctuation of the removed span
        out.append(text[pos:start])
        pos = end
        i = j + 1
    out.append(text[pos:])
    rest = "".join(out)
    rest = re.sub(r"[ \t]{2,}", " ", rest)
    rest = re.sub(r" +([,.!?;:])", r"\1", rest)
    rest = re.sub(r"\n\s*\n\s*\n+", "\n\n", rest).strip()
    rest = rest.lstrip(",.;:—–- ").strip()
    return StripResult(rest, matched, sum(marked))


# ---------------------------------------------------------------------- typed-capture check
_db_cache: dict[str, Any] = {"t": 0.0, "path": None}
_DB_CACHE_S = 30.0


def history_db() -> Path | None:
    """The Wispr history DB to watch (cached ``_DB_CACHE_S``; respects ``POLYTONE_WISPR_LOCAL``)."""
    if not truthy_env("POLYTONE_WISPR_LOCAL", True):
        return None
    now = time.monotonic()
    key = os.environ.get("WISPR_HISTORY_DIRS", "")
    if now - _db_cache["t"] > _DB_CACHE_S or _db_cache.get("key") != key:
        dbs = wispr.find_history_dbs()
        _db_cache.update(t=now, key=key, path=dbs[0] if dbs else None)
    return _db_cache["path"]


def _grace_s() -> float:
    try:
        return max(0.0, float(os.environ.get("WISPR_TYPED_GRACE_MS", DEFAULT_GRACE_MS)) / 1000.0)
    except ValueError:
        return DEFAULT_GRACE_MS / 1000.0


def check_typed(text: str, app: str | None, grace_s: float | None = None) -> StripResult | None:
    """Wispr spans in a typed message (None when Wispr isn't installed). Refreshes the index from
    the DB when it changed; with no match yet, keeps polling for up to ``grace_s`` in case the
    dictation the user just sent is still being committed."""
    path = history_db()
    if path is None:
        return None
    grace = _grace_s() if grace_s is None else grace_s
    deadline = time.monotonic() + grace
    while True:
        try:
            INSERTIONS.refresh(path)
        except Exception as e:  # noqa: BLE001 - never block typing capture on Wispr's DB
            log.warning("wispr insertion refresh failed: %s", e)
        res = strip_insertions(text, INSERTIONS.candidates(app))
        if res.matched or time.monotonic() >= deadline:
            return res
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))


# =========================================================================== live watcher
def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class WisprLiveWatcher(threading.Thread):
    def __init__(
        self, runtime: Any, db_path: Path, *, poll_s: float = POLL_S, debounce_s: float = DEBOUNCE_S,
        cap: int = TICK_CAP,
    ) -> None:
        super().__init__(name="polytone-wispr-live", daemon=True)
        self.runtime, self.db_path = runtime, Path(db_path)
        self.poll_s, self.debounce_s, self.cap = poll_s, debounce_s, cap
        self._stop_ev = threading.Event()
        self.stats: dict[str, Any] = {
            "started_at": _iso(time.time()), "reads": 0, "rows_read": 0, "accepted": 0, "rejected": 0,
            "last_pickup_at": None, "last_dictation_at": None, "last_app": None, "last_error": None,
            "last_read_ms": None, "cursor": None,
        }

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_ev.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout)

    def run(self) -> None:
        last_sig: tuple | None = None
        first = True
        while not self._stop_ev.is_set():
            sig = wispr.file_signature(self.db_path)
            if sig != last_sig:
                if not first:  # debounce: wait until the DB/WAL pair stops changing
                    for _ in range(5):
                        if self._stop_ev.wait(self.debounce_s):
                            return
                        now_sig = wispr.file_signature(self.db_path)
                        if now_sig == sig:
                            break
                        sig = now_sig
                first = False
                more = False
                try:
                    more = self.tick()
                except Exception as e:  # noqa: BLE001 - keep watching; retry on the next change
                    self.stats["last_error"] = f"{e.__class__.__name__}: {e}"
                    log.warning("wispr live read failed: %s", e)
                last_sig = None if more else sig
                if more:
                    continue  # work off a backlog without waiting for another change
            self._stop_ev.wait(self.poll_s)

    def tick(self) -> bool:
        from backend.langgraph.ingest_graph import run_ingest_batch

        t0 = time.perf_counter()
        items, rows, info = wispr.read_increment(self.db_path, self.runtime.db, self.cap)
        self.stats["last_read_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        if "error" in info:
            self.stats["last_error"] = info["error"]
            return False
        INSERTIONS.add_rows(rows)
        if rows:
            INSERTIONS.note_rowid(self.db_path, rows[-1].rowid)
        self.stats["reads"] += 1
        self.stats["rows_read"] += len(rows)
        self.stats["cursor"] = info.get("cursor")
        if items:
            res = run_ingest_batch(items, "wispr", wispr.SPOKEN_PERSONA, runtime=self.runtime)
            self.stats["accepted"] += res["accepted"]
            self.stats["rejected"] += res["rejected"]
            last = items[-1]["provenance"]
            self.stats.update(
                last_pickup_at=_iso(time.time()), last_dictation_at=last.get("timestamp"), last_app=last.get("app"),
            )
            log.info("wispr live: %d new dictation(s), accepted=%d %s", len(items), res["accepted"], res["reasons"])
        self.stats["last_error"] = None
        return bool(info.get("more"))


_active: WisprLiveWatcher | None = None
_active_lock = threading.Lock()


def live_enabled() -> bool:
    return truthy_env("POLYTONE_WISPR_LIVE", True) and history_db() is not None


def start_live(runtime: Any, **kw: Any) -> WisprLiveWatcher | None:
    """Start the live watcher when ``POLYTONE_WISPR_LIVE`` (default on) and Wispr's DB exists."""
    global _active
    if not truthy_env("POLYTONE_WISPR_LIVE", True):
        return None
    path = history_db()
    if path is None:
        return None
    with _active_lock:
        if _active is not None and _active.is_alive():
            return _active
        t = WisprLiveWatcher(runtime, path, **kw)
        t.start()
        _active = t
    log.info("wispr live: watching %s", path)
    return t


def stop_live(t: WisprLiveWatcher | None = None) -> None:
    global _active
    with _active_lock:
        t = t or _active
        if t is _active:
            _active = None
    if t is not None:
        t.stop()


def active() -> WisprLiveWatcher | None:
    t = _active
    return t if t is not None and t.is_alive() else None


def status_text() -> str | None:
    """``wispr`` source status while live, e.g. ``watching flow.sqlite; last dictation picked up
    2026-10-31T07:24:02Z (com.google.Chrome); 3 ingested live``. None when not live."""
    t = active()
    if t is None:
        return None
    s = t.stats
    parts = [f"watching {t.db_path.name}"]
    if s["last_pickup_at"]:
        app = f" ({s['last_app']})" if s["last_app"] else ""
        parts.append(f"last dictation picked up {s['last_pickup_at']}{app}")
    else:
        parts.append("no new dictation yet")
    parts.append(f"{s['accepted']} ingested live")
    if s["last_error"]:
        parts.append(f"last error: {s['last_error']}")
    return "; ".join(parts)
