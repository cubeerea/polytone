"""Hash-chained HMAC audit log (backend/security/audit.py)."""
import base64
import json
import sqlite3
import stat

import pytest

from backend.security import crypto, keys
from backend.security.audit import GENESIS, AuditLog


@pytest.fixture
def audit(tmp_path):
    a = AuditLog(tmp_path / "audit.sqlite3")
    for i in range(5):
        a.append("generate", {"persona": "Default", "chars_in": i, "chars_out": i * 2}, actor="app")
    yield a
    a.close()


def _raw(a):
    c = sqlite3.connect(a.path)
    c.isolation_level = None
    return c


def test_chain_and_verify(audit):
    v = audit.verify()
    assert v == {"ok": True, "count": 5, "first_bad_seq": None, "reason": None}
    e = audit.entries(limit=10)
    assert [x["seq"] for x in e] == [5, 4, 3, 2, 1]
    assert e[-1]["prev"] == GENESIS and e[0]["prev"] == e[1]["mac"]
    assert e[0]["detail"] == {"persona": "Default", "chars_in": 4, "chars_out": 8}
    assert [x["seq"] for x in audit.entries(limit=2, before_seq=4)] == [3, 2]
    assert stat.S_IMODE(audit.head_path.stat().st_mode) == 0o600
    assert json.loads(audit.head_path.read_text())["seq"] == 5


def test_triggers_block_update_and_delete(audit):
    c = _raw(audit)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute("UPDATE audit_log SET event = 'x' WHERE seq = 2")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        c.execute("DELETE FROM audit_log WHERE seq = 5")
    c.close()
    assert audit.verify()["ok"]


def test_tamper_detected_after_dropping_trigger(audit):
    c = _raw(audit)
    c.execute("DROP TRIGGER audit_log_no_update")
    c.execute("UPDATE audit_log SET detail = ? WHERE seq = 3", (json.dumps({"persona": "Boss"}),))
    c.close()
    v = audit.verify()
    assert v["ok"] is False and v["first_bad_seq"] == 3 and "mac" in v["reason"]


def test_truncation_detected(audit):
    c = _raw(audit)
    c.execute("DROP TRIGGER audit_log_no_delete")
    c.execute("DELETE FROM audit_log WHERE seq >= 4")
    c.close()
    v = audit.verify()
    assert v["ok"] is False and v["first_bad_seq"] == 4 and "truncated" in v["reason"]


def test_deleting_a_middle_row_detected(audit):
    c = _raw(audit)
    c.execute("DROP TRIGGER audit_log_no_delete")
    c.execute("DELETE FROM audit_log WHERE seq = 2")
    c.close()
    v = audit.verify()
    assert v["ok"] is False and v["first_bad_seq"] == 2


def test_missing_head_file_detected(audit):
    audit.head_path.unlink()
    v = audit.verify()
    assert v["ok"] is False and "missing" in v["reason"]


@pytest.mark.parametrize("content", ["[]", '"x"', "{}", '{"seq": "5", "mac": ""}', '{"seq": true}', "", "{"])
def test_malformed_head_file_reported_not_raised(audit, content):
    audit.head_path.write_text(content)
    v = audit.verify()
    assert v["ok"] is False and "unreadable" in v["reason"]


def test_head_behind_log_must_name_a_real_row(audit):
    # Crash between commit and head write: head one row behind with that row's mac is fine.
    row3 = audit.entries(limit=1, before_seq=4)[0]
    audit.head_path.write_text(json.dumps({"seq": 3, "mac": row3["mac"]}))
    assert audit.verify()["ok"] is True
    # Tail deleted and a made-up head written behind the log: detected.
    c = _raw(audit)
    c.execute("DROP TRIGGER audit_log_no_delete")
    c.execute("DELETE FROM audit_log WHERE seq >= 4")
    c.close()
    audit.head_path.write_text(json.dumps({"seq": 2, "mac": "0" * 64}))
    v = audit.verify()
    assert v["ok"] is False and v["first_bad_seq"] == 2 and "does not match" in v["reason"]


def test_key_mismatch_detected(audit, monkeypatch):
    monkeypatch.setenv(keys.ENV_KEY, base64.b64encode(b"x" * 32).decode())
    crypto.reset_cache()
    try:
        v = audit.verify()
        assert v["ok"] is False and v["first_bad_seq"] == 1 and "key" in v["reason"]
    finally:
        monkeypatch.undo()
        crypto.reset_cache()
    assert audit.verify()["ok"]


def test_long_strings_scrubbed_and_never_raises(audit):
    seq = audit.append("ingest", {"source": "typed", "oops": "x" * 5000})
    (e,) = audit.entries(limit=1)
    assert e["seq"] == seq and e["detail"]["oops"] == "<5000 chars dropped>"
    audit.close()
    assert audit.append("generate", {}) == -1  # closed connection: logged, not raised


def test_two_handles_share_one_chain(tmp_path):
    a = AuditLog(tmp_path / "audit.sqlite3")
    b = AuditLog(tmp_path / "audit.sqlite3")  # e.g. an MCP server process
    for i in range(3):
        a.append("x", {"i": i})
        b.append("y", {"i": i})
    assert a.verify() == {"ok": True, "count": 6, "first_bad_seq": None, "reason": None}
