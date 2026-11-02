"""SQLite encryption at rest + the startup migration of plaintext rows (synthetic data only)."""
import hashlib
import json
import sqlite3

from backend.ingestion import firewall
from backend.security import crypto
from backend.storage import db as dbmod
from backend.storage.db import Database, text_index

SECRET = "zebra-pancake-marmalade"  # a marker that must never appear in the file on disk


def _raw(path, sql, *args):
    c = sqlite3.connect(path)
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


def _file_bytes(path):
    out = b""
    for suffix in ("", "-wal", "-shm"):
        p = path.parent / (path.name + suffix)
        if p.exists():
            out += p.read_bytes()
    return out


def test_new_rows_are_encrypted(tmp_path):
    p = tmp_path / "e.sqlite3"
    db = Database(p)
    db.insert_draft(persona="Default", intent=f"tell {SECRET}", text=f"Hi {SECRET}.",
                    constraints={"lexicon": [SECRET]})
    db.insert_sample(text=f"i wrote {SECRET} myself", source="manual", persona="Default",
                     provenance={"subject": SECRET})
    db.insert_draft_edit(app="com.apple.mail", text=f"edited {SECRET}")
    db.close()
    for table, col in (("drafts", "intent"), ("drafts", "text"), ("drafts", "constraints_json"),
                       ("samples", "text"), ("samples", "provenance_json"), ("draft_edits", "text")):
        (v,), = _raw(p, f"SELECT {col} FROM {table}")
        assert v.startswith("enc1:"), (table, col)
    (h,), = _raw(p, "SELECT sha256 FROM samples")
    assert h == text_index(f"i wrote {SECRET} myself") and h.startswith("idx1:")
    assert SECRET.encode() not in _file_bytes(p)
    # transparent reads
    db2 = Database(p)
    assert db2.list_drafts()[0]["text"] == f"Hi {SECRET}."
    assert db2.list_drafts()[0]["constraints"] == {"lexicon": [SECRET]}
    s = db2.list_samples()[0]
    assert s["text"] == f"i wrote {SECRET} myself" and s["provenance"] == {"subject": SECRET}
    assert db2.list_draft_edits()[0]["text"] == f"edited {SECRET}"
    assert db2.sample_exists(text_index(f"  i wrote {SECRET} myself "))
    assert db2.migration == {}


def _plaintext_db(path):
    """A database in the pre-encryption format: the current schema, plaintext values, hex sha256."""
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(dbmod._SCHEMA)
    sha = lambda t: hashlib.sha256(t.strip().encode()).hexdigest()
    for i in range(3):
        text = f"draft {i} about {SECRET} for friday"
        c.execute(
            "INSERT INTO drafts(draft_id, created_at, persona, intent, text, latency_ms, constraints_json,"
            " score_json, sha256) VALUES (?,?,?,?,?,?,?,?,?)",
            (f"d{i}", f"2026-10-0{i + 1}T00:00:00.000+00:00", "Default", f"intent {SECRET}", text, 5,
             json.dumps({"lexicon": [SECRET]}), json.dumps({"score": 80.0}), sha(text)),
        )
    for i in range(4):
        text = f"sample {i}: honestly {SECRET} is fine, no rush."
        c.execute(
            "INSERT INTO samples(created_at, source, persona, text, sha256, provenance_json) VALUES (?,?,?,?,?,?)",
            (f"2026-10-0{i + 1}T00:00:00.000+00:00", "manual", "Default", text, sha(text),
             json.dumps({"source": "manual", "note": SECRET})),
        )
    c.execute("INSERT INTO draft_edits(created_at, app, text, nearest_draft_id) VALUES (?,?,?,?)",
              ("2026-10-01T00:00:00.000+00:00", "com.apple.mail", f"edit {SECRET}", "d0"))
    c.commit()
    c.close()


def test_migration_encrypts_plaintext_db(tmp_path):
    p = tmp_path / "legacy.sqlite3"
    _plaintext_db(p)
    assert SECRET.encode() in _file_bytes(p)
    db = Database(p)
    assert db.migration == {"drafts": 3, "samples": 4, "draft_edits": 1}
    db.close()
    # nothing left in plaintext: rows, free pages, WAL
    assert SECRET.encode() not in _file_bytes(p)
    assert all(v.startswith("enc1:") for (v,) in _raw(p, "SELECT text FROM samples"))
    assert all(v.startswith("idx1:") for (v,) in _raw(p, "SELECT sha256 FROM drafts"))
    wal = p.parent / (p.name + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0
    # reads are transparent and dedupe / firewall still work on the migrated rows
    db = Database(p)
    assert db.migration == {}  # idempotent: second start finds nothing
    assert sorted(d["text"] for d in db.list_drafts()) == [f"draft {i} about {SECRET} for friday" for i in range(3)]
    assert db.get_draft("d1")["constraints"] == {"lexicon": [SECRET]}
    assert db.get_draft("d1")["score"] == {"score": 80.0}
    assert {s["provenance"]["note"] for s in db.list_samples()} == {SECRET}
    ok, reason = firewall.check(f"sample 2: honestly {SECRET} is fine, no rush.", "manual", {}, db)
    assert (ok, reason) == (False, "duplicate sample")
    ok, reason = firewall.check(f"draft 1 about {SECRET} for friday", "manual", {}, db)
    assert not ok and "draft" in reason
    assert db.list_draft_edits()[0]["text"] == f"edit {SECRET}"


def test_migration_is_resumable_after_partial_run(tmp_path):
    """A half-migrated file (some rows encrypted, e.g. a crash between tables) finishes cleanly."""
    p = tmp_path / "half.sqlite3"
    _plaintext_db(p)
    c = sqlite3.connect(p)
    (text,), = c.execute("SELECT text FROM samples WHERE id = 1").fetchall()
    c.execute("UPDATE samples SET text = ?, sha256 = ? WHERE id = 1",
              (crypto.encrypt(text, "samples.text"), text_index(text)))
    c.commit()
    c.close()
    db = Database(p)
    assert db.migration["samples"] == 4  # 3 fully plaintext + 1 with plaintext provenance left
    assert len(db.list_samples()) == 4
    assert SECRET.encode() not in _file_bytes(p)


def test_migration_rolls_back_a_failing_table(tmp_path, monkeypatch):
    p = tmp_path / "fail.sqlite3"
    _plaintext_db(p)
    real = crypto.encrypt
    calls = {"n": 0}

    def flaky(value, ctx):
        calls["n"] += 1
        if ctx.startswith("samples.") and calls["n"] > 12:
            raise RuntimeError("boom")
        return real(value, ctx)

    monkeypatch.setattr(crypto, "encrypt", flaky)
    try:
        Database(p)
    except RuntimeError:
        pass
    monkeypatch.setattr(crypto, "encrypt", real)
    # the samples table is all-or-nothing: still entirely plaintext after the failure
    assert all(not v.startswith("enc1:") for (v,) in _raw(p, "SELECT text FROM samples"))
    db = Database(p)
    assert db.migration.get("samples") == 4
    assert all(v.startswith("enc1:") for (v,) in _raw(p, "SELECT text FROM samples"))


def test_purge_and_wipe(tmp_path):
    db = Database(tmp_path / "r.sqlite3")
    old, new = "2020-01-01T00:00:00.000+00:00", "2099-01-01T00:00:00.000+00:00"
    db.insert_sample(text="typed long ago by me", source="typed", persona="Default", created_at=old)
    db.insert_sample(text="spoken long ago by me", source="wispr", persona="Default", created_at=old)
    db.insert_sample(text="manual long ago by me", source="manual", persona="Default", created_at=old)
    db.insert_sample(text="typed just now by me", source="typed", persona="Default", created_at=new)
    db.insert_draft(persona="Default", intent="x", text="old draft", created_at=old)
    db.insert_draft_edit(app="a", text="old edit", created_at=old)
    out = db.purge_older_than("2026-01-01T00:00:00.000+00:00")
    assert out == {"samples": 2, "draft_edits": 1, "drafts": 1}
    assert sorted(s["text"] for s in db.list_samples()) == ["manual long ago by me", "typed just now by me"]
    assert db.wipe_user_data() == {"samples": 2, "drafts": 0, "draft_edits": 0, "corrections": 0,
                                   "constraint_snapshots": 0}
    assert db.count_samples() == 0


def test_wrong_key_refuses_to_open(tmp_path, monkeypatch):
    """A database written under one data key must not be opened (and written to) under another."""
    import base64

    import pytest

    from backend.security import keys

    p = tmp_path / "k.sqlite3"
    db = Database(p)
    db.insert_sample(text=f"honestly {SECRET} is fine", source="manual", persona="Default")
    db.insert_draft(persona="Default", intent="x", text="draft one")
    db.close()
    before = _raw(p, "SELECT text FROM samples")
    monkeypatch.setenv(keys.ENV_KEY, base64.b64encode(b"k" * 32).decode())
    crypto.reset_cache()
    try:
        with pytest.raises(crypto.KeyMismatch):
            Database(p)
        assert _raw(p, "SELECT text FROM samples") == before
        assert _raw(p, "SELECT COUNT(*) FROM drafts") == [(1,)]
        # an empty database opens fine under any key (nothing to lose)
        Database(tmp_path / "empty.sqlite3")
    finally:
        monkeypatch.undo()
        crypto.reset_cache()
    assert Database(p).list_samples()[0]["text"] == f"honestly {SECRET} is fine"
