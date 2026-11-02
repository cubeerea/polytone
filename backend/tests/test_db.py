import threading

from backend.storage.db import DEFAULT_CONFIG, Database, sha256_text


def test_config_defaults_and_update(db: Database):
    assert db.get_config() == DEFAULT_CONFIG
    cfg = db.set_config({"inject_mode": "type"}, hotkey="Alt+Space", active_persona=None)
    assert cfg["inject_mode"] == "type" and cfg["hotkey"] == "Alt+Space"
    assert cfg["active_persona"] == "Default"
    assert Database(db.path).get_config()["inject_mode"] == "type"  # persisted


def test_drafts_roundtrip(db: Database):
    d1 = db.insert_draft(persona="Boss", intent="x", text="Hello there.", latency_ms=12, constraints={"a": 1})
    d2 = db.insert_draft(persona="Friend", intent="y", text="hey!", latency_ms=5)
    assert d1["sha256"] == sha256_text("Hello there.")
    rows = db.list_drafts(10)
    assert [r["draft_id"] for r in rows] == [d2["draft_id"], d1["draft_id"]]
    assert rows[1]["constraints"] == {"a": 1} and rows[1]["score"] is None
    assert db.list_drafts(10, persona="Boss")[0]["draft_id"] == d1["draft_id"]
    db.update_draft_score(d1["draft_id"], {"score": 80.0})
    assert db.get_draft(d1["draft_id"])["score"] == {"score": 80.0}
    assert set(db.all_draft_texts()) == {"Hello there.", "hey!"}
    assert db.draft_hashes() == {sha256_text("Hello there."), sha256_text("hey!")}
    assert db.scored_drafts()[0][1] == {"score": 80.0}


def test_samples_unique_and_filters(db: Database):
    assert db.insert_sample(text="one two", source="manual", persona="Default", provenance={"k": 1})
    assert db.insert_sample(text="one two", source="gmail", persona="Boss") is None  # sha UNIQUE
    db.insert_sample(text="three", source="gmail", persona="Boss")
    assert db.sample_exists(sha256_text("one two"))
    assert not db.sample_exists(sha256_text("nope"))
    assert [s["text"] for s in db.list_samples(persona="Boss")] == ["three"]
    assert len(db.list_samples()) == 2 and db.list_samples()[1]["provenance"] == {"k": 1}
    assert db.count_samples() == 2 and db.count_samples(source="gmail") == 1
    assert db.sample_personas() == ["Boss", "Default"]


def test_ingest_runs(db: Database):
    db.record_ingest_run(source="gmail", started_at="2026-01-01T00:00:00", accepted=3, rejected=1)
    db.record_ingest_run(source="gmail", started_at="2026-01-02T00:00:00", status="error")
    runs = db.list_ingest_runs("gmail")
    assert runs[0]["status"] == "error" and runs[1]["accepted"] == 3
    assert db.list_ingest_runs("wispr") == []


def test_threads_share_one_database(tmp_path):
    db = Database(tmp_path / "mod.sqlite3")
    assert db.get_config()["active_persona"] == "Default"
    db.set_config(active_persona="Boss")

    def worker(i):
        db.insert_draft(persona="Boss", intent=str(i), text=f"draft {i}")

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(db.list_drafts(100)) == 20
    # a second Database on the same file (another process, e.g. an MCP server) sees every write
    other = Database(tmp_path / "mod.sqlite3")
    assert other.draft_watermark()[0] == 20 and other.get_config()["active_persona"] == "Boss"
    assert db.insert_sample(text="hi there", source="manual", persona="Boss")
    assert other.sample_exists(sha256_text("hi there"))
    assert other.list_samples(persona="Boss")[0]["text"] == "hi there"


def test_draft_watermark_and_since(db: Database):
    assert db.draft_watermark() == (0, 0, "") and db.drafts_since(0) == []
    a = db.insert_draft(persona="P", intent="i", text="one two three")
    b = db.insert_draft(persona="P", intent="i", text="four five six")
    n, top, top_id = db.draft_watermark()
    assert n == 2 and top >= 2 and top_id == b["draft_id"]
    rows = db.drafts_since(0)
    assert [r[2] for r in rows] == ["one two three", "four five six"] and rows[0][1] == a["sha256"]
    assert [r[2] for r in db.drafts_since(rows[0][0])] == ["four five six"]
