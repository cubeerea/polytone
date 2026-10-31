"""Live Wispr Flow ingestion: rowid cursor, read-only snapshots, the live watcher, and the
typing-capture check that keeps Wispr's AI-formatted insertions out of ``typed`` samples.

Every test builds its own Wispr-shaped ``flow.sqlite`` (WAL mode, the real column names incl.
blob columns) in a temp dir; nothing touches the real Wispr install or ``data/``."""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from backend.ingestion import sources
from backend.ingestion.text import tokenize
from backend.mcp_servers import wispr, wispr_live

SLACK = "com.tinyspeck.slackmacgap"
SCHEMA = """
CREATE TABLE `History` (`transcriptEntityId` VARCHAR(36) NOT NULL UNIQUE PRIMARY KEY, `asrText` TEXT,
 `formattedText` TEXT, `editedText` TEXT, `timestamp` DATETIME, `audio` BLOB, `screenshot` BLOB,
 `additionalContext` JSON, `status` VARCHAR(255), `app` VARCHAR(255), `url` VARCHAR(255),
 `duration` FLOAT, `numWords` INTEGER, `toneMatchedText` TEXT, `isArchived` TINYINT(1) NOT NULL DEFAULT 0,
 `pastedText` TEXT, `defaultAsrText` TEXT, `serverFinalizedText` TEXT, `editedTextUnbounded` TEXT);
CREATE INDEX `idx_history_timestamp_archived_status` ON `History` (`timestamp`, `isArchived`, `status`);
"""


def ts(dt: datetime | None = None, **delta) -> str:
    dt = (dt or datetime.now(UTC)) + timedelta(**delta)
    return wispr.wispr_ts(dt.timestamp())


class FakeWispr:
    """A Wispr-shaped history DB kept open in WAL mode (like the running app)."""

    def __init__(self, root: Path) -> None:
        self.dir = root / "Wispr Flow"
        self.dir.mkdir()
        self.path = self.dir / "flow.sqlite"
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def add(self, asr: str | None, formatted: str | None = None, *, stamp: str | None = None,
            status: str | None = "formatted", app: str | None = SLACK, archived: int = 0,
            pasted: str | None = "same", url: str | None = None, duration: float = 4.0) -> int:
        if pasted == "same":
            pasted = None if formatted is None else formatted + " "
        cur = self.conn.execute(
            "INSERT INTO History (transcriptEntityId, asrText, formattedText, timestamp, audio, screenshot,"
            " additionalContext, status, app, url, duration, isArchived, pastedText, serverFinalizedText)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), asr, formatted, stamp or ts(), b"\0" * 2048, b"\0" * 2048, '{"big": 1}',
             status, app, url, duration, archived, pasted, formatted),
        )
        self.conn.commit()
        return cur.lastrowid

    def close(self) -> None:
        self.conn.close()


@pytest.fixture
def fake(tmp_path, monkeypatch):
    w = FakeWispr(tmp_path)
    monkeypatch.setenv("WISPR_HISTORY_DIRS", str(w.dir))
    monkeypatch.delenv("POLYTONE_WISPR_LOCAL", raising=False)
    monkeypatch.delenv("POLYTONE_WISPR_LIVE", raising=False)
    monkeypatch.setenv("WISPR_TYPED_GRACE_MS", "0")
    wispr_live.INSERTIONS.clear()
    wispr_live._db_cache.update(t=0.0, path=None, key=None)
    yield w
    wispr_live.stop_live()
    wispr_live.INSERTIONS.clear()
    w.close()


def _enable_capture(runtime):
    runtime.db.set_config(capture_enabled=True)


# ------------------------------------------------------------------ reader / cursor
def test_wal_snapshot_sees_uncheckpointed_rows_and_leaves_source_alone(fake, runtime):
    fake.add("this row only lives in the wal file so far")
    assert Path(str(fake.path) + "-wal").stat().st_size > 0
    sig = wispr.file_signature(fake.path)
    items, _ = wispr.collect_local(runtime.settings, db=runtime.db)
    assert [i["text"] for i in items] == ["this row only lives in the wal file so far"]
    assert wispr.file_signature(fake.path) == sig  # db + wal untouched


def test_rowid_cursor_catches_rows_with_older_start_timestamps(fake, runtime):
    base = datetime.now(UTC)
    fake.add("a short dictation that started last", stamp=ts(base))
    first, _ = wispr.collect_local(runtime.settings, db=runtime.db)
    assert len(first) == 1
    # a long dictation that STARTED earlier but finished (= was inserted) later
    fake.add("a long dictation that started first but finished later", stamp=ts(base, seconds=-90))
    second, info = wispr.collect_local(runtime.settings, db=runtime.db)
    assert [i["text"] for i in second] == ["a long dictation that started first but finished later"]
    assert info["cursor"] == 2
    assert wispr.collect_local(runtime.settings, db=runtime.db)[0] == []


def test_cursor_migrates_from_old_timestamp_cursor(fake, runtime):
    base = datetime(2026, 10, 1, tzinfo=UTC)
    for i in range(4):
        fake.add(f"dictation number {i} with enough words", stamp=ts(base, minutes=i))
    runtime.db.set_config({"wispr.history_last_ts": ts(base, minutes=1)})  # rows 1-2 already ingested
    items, _ = wispr.collect_local(runtime.settings, db=runtime.db)
    assert [i["provenance"]["rowid"] for i in items] == [3, 4]
    assert runtime.db.get_config()["wispr.history_rowid"] == "4"


def test_only_named_columns_are_selected_never_blobs(fake, runtime, monkeypatch):
    fake.add("checking which columns we read", "Checking which columns we read.")
    statements: list[str] = []
    real = wispr._connect_ro

    def traced(path):
        conn = real(path)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(wispr, "_connect_ro", traced)
    wispr.collect_local(runtime.settings, db=runtime.db)
    wispr_live.INSERTIONS.refresh(fake.path)
    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT") and "History" in s]
    assert selects, statements
    for s in selects:
        assert "*" not in s.split("FROM")[0], s
        for blob in ("audio", "screenshot", "additionalContext"):
            assert blob not in s, s


def test_formatted_text_is_never_ingested(fake, runtime):
    fake.add("um so yeah lets move standup to ten", "So yeah, let's move standup to 10.")
    fake.add(None, "Only the formatted text exists for this one.", status="formatted")
    fake.add("", "Blank raw transcript, formatted text present.")
    fake.add("this one got dismissed by the user", "This one got dismissed.", status="dismissed")
    fake.add("um uh hmm", "Um.")  # filler-only transcript
    r = sources.run_source("wispr", runtime)
    assert r["accepted"] == 1
    samples = runtime.db.list_samples(source="wispr")
    assert [s["text"] for s in samples] == ["um so yeah lets move standup to ten"]
    blob = repr(samples[0]["provenance"])
    assert "So yeah" not in blob and "formatted" not in blob.lower().replace("formattedtext", "")


def test_pending_row_filled_in_later_is_picked_up(fake, runtime):
    rid = fake.add(None, None, status=None, app=None, pasted=None)  # inserted first, filled in later
    assert wispr.collect_local(runtime.settings, db=runtime.db)[0] == []
    fake.conn.execute("UPDATE History SET asrText=?, status='formatted', app=? WHERE rowid=?",
                      ("the text arrived after the row was inserted", SLACK, rid))
    fake.conn.commit()
    items, _ = wispr.collect_local(runtime.settings, db=runtime.db)
    assert [i["text"] for i in items] == ["the text arrived after the row was inserted"]
    assert wispr.collect_local(runtime.settings, db=runtime.db)[0] == []


# ------------------------------------------------------------------ live watcher
def _wait(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def test_live_watcher_ingests_appended_rows(fake, runtime):
    fake.add("an older dictation from before the server started")
    t = wispr_live.WisprLiveWatcher(runtime, fake.path, poll_s=0.05, debounce_s=0.05)
    t.start()
    try:
        assert _wait(lambda: runtime.db.count_samples(source="wispr") == 1)  # backlog on start
        fake.add("ok so the live watcher should pick this one up quickly", "OK, so the live watcher…",
                 url="https://example.com/doc")
        t0 = time.monotonic()
        assert _wait(lambda: runtime.db.count_samples(source="wispr") == 2, timeout=4)
        assert time.monotonic() - t0 < 3
    finally:
        t.stop()
    newest = runtime.db.list_samples(source="wispr")
    s = next(x for x in newest if x["text"].startswith("ok so the live"))
    p = s["provenance"]
    assert s["persona"] == "Default"
    assert p["raw_transcript"] is True and p["kind"] == "spoken" and p["origin"] == "wispr_local_history"
    assert p["rowid"] == 2 and p["app"] == SLACK and p["url"] == "https://example.com/doc" and p["transcript_id"]
    assert t.stats["accepted"] == 2 and t.stats["last_pickup_at"]
    assert len(wispr_live.INSERTIONS) == 2  # fed the typed-capture index too


def test_live_status_and_health(fake, runtime, client):
    assert wispr_live.start_live(runtime, poll_s=0.05, debounce_s=0.05) is not None
    fake.add("a dictation for the status line to report on")
    assert _wait(lambda: runtime.db.count_samples(source="wispr") == 1)
    d = sources.describe_source("wispr", runtime)
    assert d["enabled"] is True and d["items_ingested"] == 1
    assert d["status"].startswith("live (watching flow.sqlite; last dictation picked up ")
    assert SLACK in d["status"] and "1 ingested live" in d["status"]
    assert client.get("/health").json()["wispr_live"] is True
    wispr_live.stop_live()
    assert client.get("/health").json()["wispr_live"] is False
    assert sources.describe_source("wispr", runtime)["status"].startswith("ready (local history")


def test_live_disabled_by_env(fake, runtime, monkeypatch):
    monkeypatch.setenv("POLYTONE_WISPR_LIVE", "0")
    assert wispr_live.start_live(runtime) is None
    monkeypatch.setenv("POLYTONE_WISPR_LIVE", "1")
    monkeypatch.setenv("POLYTONE_WISPR_LOCAL", "0")
    wispr_live._db_cache.update(t=0.0)
    assert wispr_live.start_live(runtime) is None


# ------------------------------------------------------------------ typed ↔ wispr
PASTED = "Sounds good, I'll send the deck over tonight after the call."


def test_typed_message_with_wispr_text_keeps_only_the_typed_part(fake, runtime):
    _enable_capture(runtime)
    fake.add("sounds good ill send the deck over tonight after the call", PASTED)
    msg = PASTED + " also can you loop in priya on the budget thread"
    r = sources.ingest_typed(runtime, msg, SLACK, "Slack")  # index cold: reads the DB on demand
    assert r["accepted"] == 1, r
    [s] = runtime.db.list_samples(source="typed")
    assert s["text"] == "also can you loop in priya on the budget thread"
    assert s["provenance"]["wispr_removed"] == {"rowids": [1], "tokens": 11}


def test_typed_message_that_is_wispr_output_is_rejected(fake, runtime):
    _enable_capture(runtime)
    fake.add("sounds good ill send the deck over tonight after the call", PASTED)
    r = sources.ingest_typed(runtime, PASTED, SLACK)
    assert r["accepted"] == 0 and r["reasons"][0].startswith("wispr_inserted")
    # lightly edited after insertion (≥ 80% shingle overlap) + a short typed tail
    edited = "Sounds good, I'll send the deck over tonight after the meeting. thx"
    r = sources.ingest_typed(runtime, edited, SLACK)
    assert r["accepted"] == 0 and r["reasons"][0].startswith("wispr_inserted"), r
    assert runtime.db.count_samples(source="typed") == 0


def test_typed_wispr_check_is_per_app_and_time_window(fake, runtime):
    _enable_capture(runtime)
    fake.add("sounds good ill send the deck over tonight after the call", PASTED)
    other = sources.ingest_typed(runtime, PASTED, "com.apple.mail")
    assert other["accepted"] == 1  # Wispr inserted it into Slack, not Mail
    old = "Yes we can absolutely do the offsite in march next year."
    fake.add("yes we can absolutely do the offsite in march next year", old, stamp=ts(minutes=-45))
    assert sources.ingest_typed(runtime, old, SLACK)["accepted"] == 1  # older than 30 min


def test_typed_waits_for_a_dictation_still_being_committed(fake, runtime, monkeypatch):
    _enable_capture(runtime)
    monkeypatch.setenv("WISPR_TYPED_GRACE_MS", "2500")
    sources.ingest_typed(runtime, "warm the insertion index up with this message", SLACK)
    late = threading.Timer(0.4, lambda: fake.add("ok see you at the offsite on friday then", "OK, see you at the offsite on Friday then."))
    late.start()
    t0 = time.monotonic()
    r = sources.ingest_typed(runtime, "OK, see you at the offsite on Friday then.", SLACK)
    late.join()
    assert r["accepted"] == 0 and r["reasons"][0].startswith("wispr_inserted"), r
    assert time.monotonic() - t0 < 2.5


def test_typed_without_wispr_is_unchanged(runtime, monkeypatch, tmp_path):
    monkeypatch.setenv("WISPR_HISTORY_DIRS", str(tmp_path / "nowhere"))
    wispr_live._db_cache.update(t=0.0, key=None)
    _enable_capture(runtime)
    assert wispr_live.check_typed("hello there my friend", SLACK) is None
    assert sources.ingest_typed(runtime, "hello there my friend how are you", SLACK)["accepted"] == 1


def test_strip_insertions_unit():
    ins = wispr_live.Insertion(1, SLACK, time.time(), (("i", "will", "be", "there", "at", "noon"),))
    r = wispr_live.strip_insertions("hey! I will be there at noon. bring the slides pls", [ins])
    assert r.matched == [1] and r.text == "hey! bring the slides pls"
    assert wispr_live.strip_insertions("nothing in common here at all", [ins]).matched == []
    short = wispr_live.Insertion(2, SLACK, time.time(), (("yes",),))
    assert wispr_live.strip_insertions("yes I typed this myself", [short]).matched == []  # 1 token, not whole
    assert wispr_live.strip_insertions("Yes.", [short]).text == ""


def test_strip_prefers_verbatim_pasted_text_over_raw_coverage():
    raw = tuple(tokenize("um so i think we should push the launch to friday because the deck is not ready yet"))
    pasted = tuple(tokenize("So I think we should push the launch to Friday because the deck isn't ready yet."))
    ins = wispr_live.Insertion(7, SLACK, time.time(), tuple(sorted({raw, pasted}, key=len, reverse=True)))
    text = "So I think we should push the launch to Friday, because the deck isn't ready yet."
    r = wispr_live.strip_insertions(text, [ins])
    assert r.matched == [7] and r.text == ""


# ------------------------------------------------------------------ real-world row lifecycles
def _update(fake, rowid, asr, formatted, status="formatted"):
    """Wispr filling in a row it inserted earlier (from its own connection, WAL mode)."""
    pasted = None if formatted is None else formatted + " "
    fake.conn.execute(
        "UPDATE History SET asrText=?, formattedText=?, pastedText=?, serverFinalizedText=?, status=?,"
        " app=? WHERE rowid=?", (asr, formatted, pasted, formatted, status, SLACK, rowid))
    fake.conn.commit()


def test_row_inserted_with_an_interim_status_then_filled_is_picked_up(fake, runtime):
    # a row written when the dictation starts, with a non-final status and no text yet
    rid = fake.add(None, None, status="transcribing", pasted=None)
    assert wispr.collect_local(runtime.settings, db=runtime.db)[0] == []
    _update(fake, rid, "the transcript showed up once the status was final", "The transcript showed up.")
    items, _ = wispr.collect_local(runtime.settings, db=runtime.db)
    assert [i["text"] for i in items] == ["the transcript showed up once the status was final"]
    # a final status with no text (no speech detected) is not re-checked forever
    fake.add("", "", status="no_audio", pasted=None)
    wispr.collect_local(runtime.settings, db=runtime.db)
    assert not [k for k in wispr._pending if k[0] == str(fake.path)]


def test_live_watcher_default_timing_picks_up_new_and_filled_rows_within_2s(fake, runtime):
    """Default poll (1 s) + debounce (300 ms), rows written by another connection in WAL mode."""
    t = wispr_live.WisprLiveWatcher(runtime, fake.path)
    t.start()
    try:
        assert _wait(lambda: t.stats["reads"] >= 1, timeout=3)
        writer = sqlite3.connect(fake.path)  # a second writer connection, like Wispr's
        writer.execute(
            "INSERT INTO History (transcriptEntityId, asrText, formattedText, timestamp, status, app, duration,"
            " isArchived, pastedText) VALUES (?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), "a brand new dictation arriving while the server runs", "A brand new dictation.",
             ts(), "formatted", SLACK, 3.0, 0, "A brand new dictation. "))
        writer.commit()
        t0 = time.monotonic()
        assert _wait(lambda: runtime.db.count_samples(source="wispr") == 1, timeout=2.5)
        assert time.monotonic() - t0 < 2.0
        # inserted empty first (dictation running), filled in ~1.2 s later
        cur = writer.execute(
            "INSERT INTO History (transcriptEntityId, timestamp, isArchived) VALUES (?,?,0)",
            (str(uuid.uuid4()), ts()))
        writer.commit()
        rid = cur.lastrowid
        time.sleep(1.2)
        assert runtime.db.count_samples(source="wispr") == 1
        writer.execute("UPDATE History SET asrText=?, status='formatted', app=? WHERE rowid=?",
                       ("and this one was filled in after it was inserted", SLACK, rid))
        writer.commit()
        t0 = time.monotonic()
        assert _wait(lambda: runtime.db.count_samples(source="wispr") == 2, timeout=2.5)
        assert time.monotonic() - t0 < 2.0
        writer.close()
    finally:
        t.stop()
    assert t.stats["last_error"] is None


def _rebuild_table(fake):
    """What a Sequelize SQLite migration (changeColumn / removeColumn) does to History: copy into a
    backup table, drop, recreate, copy back. History's primary key is TEXT, so rowids renumber."""
    c = fake.conn
    c.execute("CREATE TABLE History_backup AS SELECT * FROM History ORDER BY rowid")
    c.execute("DROP TABLE History")
    c.executescript(SCHEMA)
    c.execute("INSERT INTO History SELECT * FROM History_backup")
    c.execute("DROP TABLE History_backup")
    c.commit()


def test_cursor_survives_rowids_renumbered_by_a_migration(fake, runtime):
    """The cursor is re-found by the transcript id it points at."""
    for i in range(3):
        fake.add(f"older dictation number {i} with enough words")
    assert len(wispr.collect_local(runtime.settings, db=runtime.db)[0]) == 3
    fake.conn.execute("DELETE FROM History WHERE rowid IN (1, 2)")
    fake.conn.commit()
    _rebuild_table(fake)
    assert [r[0] for r in fake.conn.execute("SELECT rowid FROM History")] == [1]  # renumbered
    fake.add("the first dictation after the migration")
    fake.add("and the second dictation after the migration")
    items, info = wispr.collect_local(runtime.settings, db=runtime.db)
    assert [i["text"] for i in items] == ["the first dictation after the migration",
                                          "and the second dictation after the migration"]
    assert info["cursor"] == 3


def test_cursor_past_the_end_after_the_db_was_replaced(fake, runtime):
    base = datetime.now(UTC) - timedelta(hours=2)
    for i in range(3):
        fake.add(f"dictation {i} from the old install of wispr", stamp=ts(base, minutes=i))
    wispr.collect_local(runtime.settings, db=runtime.db)
    fake.conn.execute("DELETE FROM History")
    fake.conn.commit()
    fake.conn.execute("VACUUM")
    fake.add("the very first dictation in a fresh install")  # rowid 1, newer timestamp
    items, info = wispr.collect_local(runtime.settings, db=runtime.db)
    assert [i["text"] for i in items] == ["the very first dictation in a fresh install"]
    assert info["cursor"] == 1


def test_typed_check_sees_a_row_filled_in_after_the_index_read_it(fake, runtime):
    """Watcher off: the on-demand index read a row while Wispr had only inserted it; the
    insertion text arrives later and must still be recognised."""
    _enable_capture(runtime)
    rid = fake.add(None, None, status=None, app=None, pasted=None)
    sources.ingest_typed(runtime, "warm up the insertion index with this message", SLACK)
    _update(fake, rid, "ok see you at the offsite on friday then", "OK, see you at the offsite on Friday then.")
    r = sources.ingest_typed(runtime, "OK, see you at the offsite on Friday then.", SLACK)
    assert r["accepted"] == 0 and r["reasons"][0].startswith("wispr_inserted"), r


def test_ingest_typed_route_strips_wispr_and_respects_the_switch(fake, client, runtime):
    fake.add("sounds good ill send the deck over tonight after the call", PASTED)
    body = {"text": PASTED + " also loop in priya on the budget thread please", "app": SLACK}
    off = client.post("/ingest/typed", json=body).json()
    assert off["accepted"] == 0 and off["reasons"][0].startswith("typing capture is disabled")
    client.put("/config", json={"capture_enabled": True})
    on = client.post("/ingest/typed", json=body).json()
    assert on["accepted"] == 1, on
    [s] = runtime.db.list_samples(source="typed")
    assert s["text"] == "also loop in priya on the budget thread please"
    only = client.post("/ingest/typed", json={"text": PASTED, "app": SLACK}).json()
    assert only["accepted"] == 0 and only["reasons"][0].startswith("wispr_inserted")
    # after a Polytone inject: stored for edit learning, never learned, no Wispr wait
    t0 = time.monotonic()
    inj = client.post("/ingest/typed", json={**body, "after_inject": True}).json()
    assert inj["reasons"] == ["after_polytone_inject: stored for edit learning"]
    assert time.monotonic() - t0 < 1.0
    assert len(runtime.db.list_draft_edits()) == 1
    assert runtime.db.count_samples(source="typed") == 1


def test_typed_check_after_rowids_were_renumbered(fake, runtime):
    _enable_capture(runtime)
    for i in range(3):
        fake.add(f"older dictation number {i} with enough words", f"Older dictation {i}.")
    sources.ingest_typed(runtime, "warm up the insertion index with this message", SLACK)
    fake.conn.execute("DELETE FROM History WHERE rowid IN (1, 2)")
    fake.conn.commit()
    _rebuild_table(fake)
    fake.add("sounds good ill send the deck over tonight after the call", PASTED)  # rowid 2 now
    r = sources.ingest_typed(runtime, PASTED, SLACK)
    assert r["accepted"] == 0 and r["reasons"][0].startswith("wispr_inserted"), r
