"""File watcher source: txt / mbox (outbound only) / SQLite dumps, idempotency, live watching."""
from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path

import pytest

from backend.ingestion import sources
from backend.mcp_servers import _common
from backend.mcp_servers import file_watcher as fw

FIXTURES = Path(__file__).parent / "fixtures"
ME = "hank@example.com"


@pytest.fixture
def inbox(runtime, monkeypatch):
    monkeypatch.setenv("POLYTONE_USER_EMAILS", ME)
    d = Path(runtime.settings.polytone_watch_dir)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _drop(inbox: Path, name: str, as_name: str | None = None) -> Path:
    dst = inbox / (as_name or name)
    shutil.copy(FIXTURES / name, dst)
    return dst


def test_txt_whole_and_split(inbox):
    short = fw.parse_txt(FIXTURES / "note.txt")
    assert len(short) == 1 and short[0]["text"].startswith("hey — just a quick note")
    long = fw.parse_txt(FIXTURES / "long_essay.txt")
    assert len(long) == 30 and long[3]["text"].startswith("paragraph 3:")
    assert long[0]["provenance"]["kind"] == "txt" and long[0]["provenance"]["direction"] == "outbound"


def test_mbox_only_from_user(inbox):
    items, counts = fw.parse_mbox(FIXTURES / "mixed.mbox", {ME})
    texts = [i["text"] for i in items]
    assert counts == {"messages": 4, "inbound_skipped": 2, "empty": 0}
    assert texts == [
        "sounds good — i'll bring the slides tomorrow. honestly can't wait to show you.",
        "quick heads up: we're moving standup to 10am on friday.",
    ]
    assert not any("inbound" in t or "SALE" in t or "Pal" in t for t in texts)
    assert items[0]["provenance"]["message_id"] == "<out-1@example.com>"
    with pytest.raises(fw.Skip):
        fw.parse_mbox(FIXTURES / "mixed.mbox", set())  # no user emails → nothing imported


def test_imessage_chat_db(inbox):
    items, info = fw.parse_sqlite(FIXTURES / "chat.db", {ME})
    assert info["shape"] == "imessage"
    assert [i["text"] for i in items] == [
        "yeah! running a bit late though, save me a seat.",
        "honestly the food there is so good. we should go back next week.",
    ]


def test_generic_dump_by_sender(inbox):
    items, info = fw.parse_sqlite(FIXTURES / "generic_dump.sqlite", {ME})
    assert info["shape"] == "generic" and info["by"] == "sender"
    texts = [i["text"] for i in items]
    assert len(texts) == 2 and not any("thanks for the quick" in t for t in texts)


def test_generic_dump_by_flag_and_unknown(tmp_path):
    p = tmp_path / "x.sqlite"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE msgs (body TEXT, outgoing INTEGER)")
    c.executemany("INSERT INTO msgs VALUES (?, ?)", [("mine for sure.", 1), ("theirs.", 0)])
    c.commit()
    c.close()
    items, info = fw.parse_sqlite(p, set())
    assert [i["text"] for i in items] == ["mine for sure."] and info["by"] == "outgoing"
    q = tmp_path / "y.db"
    c = sqlite3.connect(q)
    c.execute("CREATE TABLE notes (content TEXT, created TEXT)")
    c.commit()
    c.close()
    with pytest.raises(fw.Skip, match="no is_from_me/sender"):
        fw.parse_sqlite(q, set())


def test_collect_idempotent_via_source_hook(runtime, inbox):
    _drop(inbox, "note.txt")
    _drop(inbox, "mixed.mbox")
    _drop(inbox, "chat.db")
    (inbox / "image.png").write_bytes(b"\x89PNG....")
    d = sources.describe_source("watcher", runtime)
    assert d["enabled"] is True and d["status"].startswith("ready")
    r = sources.run_source("watcher", runtime)
    assert r["accepted"] == 5, r  # 1 txt + 2 mbox (outbound) + 2 iMessage
    samples = runtime.db.list_samples(source="watcher")
    assert not any("inbound" in s["text"] or "are you coming" in s["text"] for s in samples)
    r2 = sources.run_source("watcher", runtime)
    assert r2["accepted"] == 0 and r2["rejected"] == 0  # already processed (sha256)
    _items, report = fw.scan(runtime.settings, runtime.db)
    status = {x["file"]: x["status"] for x in report}
    assert status["image.png"] == "skipped" and status["note.txt"] == "already processed"


def test_scan_and_ingest_reports(runtime, inbox):
    _drop(inbox, "long_essay.txt")
    out = fw.scan_and_ingest(runtime)
    assert out["accepted"] == 30 and out["files"][0]["status"] == "parsed"


def test_user_emails_sources(runtime, monkeypatch):
    monkeypatch.delenv("POLYTONE_USER_EMAILS", raising=False)
    runtime.db.set_config({"user_emails": "A@Example.com, b@example.com", "gmail.address": "g@example.com"})
    assert _common.user_emails(runtime.settings) == {"a@example.com", "b@example.com", "g@example.com"}


def test_watcher_thread_picks_up_new_files(runtime, inbox):
    t = fw.WatcherThread(runtime, debounce=0.2)
    t.start()
    try:
        time.sleep(0.5)
        _drop(inbox, "note.txt", "dropped.txt")
        deadline = time.time() + 15
        while time.time() < deadline and runtime.db.count_samples(source="watcher") == 0:
            time.sleep(0.2)
        assert runtime.db.count_samples(source="watcher") == 1
    finally:
        t.stop()
    assert not t.is_alive()


def test_daemon_starts_and_stops_watcher(runtime, inbox, monkeypatch):
    from fastapi.testclient import TestClient

    from backend.daemon.server import create_app

    monkeypatch.setenv("POLYTONE_WATCHER", "1")
    with TestClient(create_app(runtime)) as c:
        c.app.state.warmup.join(5)  # the watcher starts in the background warm-up thread
        th = fw._active_thread
        assert th is not None and th.is_alive()
        assert "watching" in {s["name"]: s for s in c.get("/ingest/sources").json()}["watcher"]["status"]
    assert not th.is_alive() and fw._active_thread is None


def test_daemon_watcher_disabled(runtime, inbox, monkeypatch):
    from fastapi.testclient import TestClient

    from backend.daemon.server import create_app

    monkeypatch.setenv("POLYTONE_WATCHER", "0")
    with TestClient(create_app(runtime)) as c:
        c.app.state.warmup.join(5)
        assert fw._active_thread is None
