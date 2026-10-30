"""Wispr source: tolerant WS parsing, a local fake WebSocket server, and a fixture history DB."""
from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from backend.ingestion import sources
from backend.mcp_servers import wispr

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def history_dir(tmp_path, monkeypatch):
    d = tmp_path / "Wispr Flow"
    d.mkdir()
    shutil.copy(FIXTURES / "wispr_flow.sqlite", d / "flow.sqlite")
    (d / "backups").mkdir()
    shutil.copy(FIXTURES / "wispr_flow.sqlite", d / "backups" / "backup-old.sqlite")
    monkeypatch.setenv("WISPR_HISTORY_DIRS", str(d))
    monkeypatch.delenv("POLYTONE_WISPR_LOCAL", raising=False)
    return d


@pytest.fixture
def no_local(tmp_path, monkeypatch):
    monkeypatch.setenv("WISPR_HISTORY_DIRS", str(tmp_path / "nowhere"))


# ------------------------------------------------------------------ parsing
@pytest.mark.parametrize(
    "msg,expected",
    [
        ({"text": "hello there"}, "hello there"),
        ({"transcript": "raw words", "formatted": "Raw words."}, "raw words"),
        ({"raw_text": "um raw", "text": "Polished?"}, "um raw"),
        ({"asrText": "asr words", "formattedText": "ASR words."}, "asr words"),
        ({"type": "transcript", "data": {"rawText": "nested raw"}}, "nested raw"),
        ({"event": "x", "payload": {"transcript": {"raw_transcript": "deep raw"}}}, "deep raw"),
        ({"formatted": "Only polished.", "formatted_text": "Only polished."}, None),
        ({"polishedText": "AI text"}, None),
        ({"text": "partial words", "is_final": False}, None),
        ({"type": "partial", "text": "partial"}, None),
        ({"type": "ping"}, None),
        ("not json at all", None),
        ("[1, 2]", None),
    ],
)
def test_parse_message(msg, expected):
    raw = msg if isinstance(msg, str) else json.dumps(msg)
    got = wispr.parse_message(raw)
    assert (got["text"] if got else None) == expected


# ------------------------------------------------------------------ websocket
def _fake_server(messages, *, drop_first=False):
    """Local WS server that checks the bearer key and pushes ``messages`` then closes."""
    from websockets.asyncio.server import serve

    state = {"conns": 0, "auth": []}

    async def handler(ws):
        state["conns"] += 1
        state["auth"].append(ws.request.headers.get("Authorization"))
        if drop_first and state["conns"] == 1:
            await ws.close()  # force a reconnect
            return
        for m in messages:
            await ws.send(m if isinstance(m, str) else json.dumps(m))
        await asyncio.sleep(0.05)

    return serve(handler, "127.0.0.1", 0), state


def test_ws_stream_reconnects_and_filters():
    msgs = [
        {"type": "transcript", "transcript": "so yeah lets just ship it", "formatted": "So yeah, let's just ship it."},
        {"type": "partial", "text": "so ye"},
        {"formatted_text": "Only AI text."},
        {"data": {"raw_text": "i'll call you after lunch", "id": "abc"}},
    ]

    async def main():
        server_cm, state = _fake_server(msgs, drop_first=True)
        async with server_cm as server:
            port = server.sockets[0].getsockname()[1]
            got = []
            stats = await wispr.stream_transcripts(
                f"ws://127.0.0.1:{port}", "k-123", got.append, max_items=2, duration=10,
                backoff_initial=0.05,
            )
            return got, stats, state

    got, stats, state = asyncio.run(asyncio.wait_for(main(), 20))
    assert [g["text"] for g in got] == ["so yeah lets just ship it", "i'll call you after lunch"]
    assert all(g["provenance"]["source"] == "wispr" and g["provenance"]["raw_transcript"] is True for g in got)
    assert got[1]["provenance"]["transcript_id"] == "abc"
    assert state["conns"] >= 2 and stats["connects"] >= 2  # reconnected after the server dropped us
    assert stats["ignored"] == 2
    assert state["auth"][0] == "Bearer k-123"


def test_ws_unreachable_backs_off_and_gives_up():
    async def main():
        return await wispr.stream_transcripts(
            "ws://127.0.0.1:9", "k", lambda i: None, max_attempts=3, backoff_initial=0.01, open_timeout=1
        )

    stats = asyncio.run(asyncio.wait_for(main(), 20))
    assert stats["failures"] == 3 and stats["received"] == 0 and stats["last_error"]


def test_collect_ws_via_settings(runtime, no_local):
    async def run_server_and_collect():
        server_cm, _ = _fake_server([{"text": "dictated line one is here"}, {"text": "and a second line"}])
        async with server_cm as server:
            port = server.sockets[0].getsockname()[1]
            runtime.settings.wispr_ws_url = f"ws://127.0.0.1:{port}"
            runtime.settings.wispr_api_key = "k"
            return await asyncio.to_thread(wispr.collect_ws, runtime.settings, 3.0, 2)

    items, _stats = asyncio.run(asyncio.wait_for(run_server_and_collect(), 20))
    assert [i["text"] for i in items] == ["dictated line one is here", "and a second line"]
    assert wispr.is_enabled(runtime.settings) and "websocket configured" in wispr.status(runtime.settings)


# ------------------------------------------------------------------ local history
def test_inspect_schema_prefers_raw_column(history_dir):
    import sqlite3

    conn = sqlite3.connect(history_dir / "flow.sqlite")
    s = wispr.inspect_schema(conn)
    conn.close()
    assert s["table"] == "History" and s["text_col"] == "asrText" and s["ts_col"] == "timestamp"
    assert {"formattedText", "editedText", "toneMatchedText", "pastedText"} <= set(s["polished_cols_ignored"])


def test_local_history_raw_only_incremental_and_readonly(runtime, history_dir):
    db_file = history_dir / "flow.sqlite"
    before = db_file.read_bytes()
    assert wispr.find_history_dbs() == [db_file]  # backups/ ignored
    items, info = wispr.collect_local(runtime.settings, db=runtime.db)
    texts = [i["text"] for i in items]
    assert texts == [
        "um so i think we should uh push the launch to friday",
        "yeah no that works for me lets do it",
        "ok cool ill send the deck over tonight",
    ]  # raw asrText; dismissed / archived / polished-only rows skipped
    assert not any("." in t or "I think" in t for t in texts)
    p = items[0]["provenance"]
    assert p["source"] == "wispr" and p["raw_transcript"] is True and p["field"] == "asrText"
    assert info["ignored_cols"]
    assert runtime.db.get_config()["wispr.history_last_ts"] == "2026-09-11 09:00:00.000 +00:00"
    again, _ = wispr.collect_local(runtime.settings, db=runtime.db)
    assert again == []  # incremental
    assert db_file.read_bytes() == before  # original untouched


def test_wispr_source_run_through_firewall(runtime, history_dir):
    d = sources.describe_source("wispr", runtime)
    assert d["enabled"] is True and "local history" in d["status"]
    r = sources.run_source("wispr", runtime)
    assert r["accepted"] == 3
    assert runtime.db.count_samples(source="wispr") == 3


def test_needs_credentials_when_nothing_configured(runtime, no_local):
    assert wispr.is_enabled(runtime.settings) is False
    assert wispr.status(runtime.settings).startswith("needs credentials")
    assert wispr.collect(runtime.settings) == []
