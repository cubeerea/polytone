"""Local API auth, sessions, audit endpoints, wipe/retention, file hygiene, Keychain fallbacks."""
import json
import os
import sqlite3
import stat

import pytest

from backend.tests.conftest import HUMAN_SAMPLES, TEST_APP_TOKEN

SECRET = "quokka-saxophone-lantern"


@pytest.fixture
def anon(client):
    """Same app (same sessions) as ``client``, but sends no token by default."""
    from fastapi.testclient import TestClient

    return TestClient(client.app, base_url=str(client.base_url))


def _bearer(t):
    return {"Authorization": f"Bearer {t}"}


def _events(client):
    return [e["event"] for e in client.get("/audit?limit=1000").json()["entries"]]


# ---------------------------------------------------------------------------- auth basics
def test_health_open_everything_else_locked(anon):
    h = anon.get("/health")
    assert h.status_code == 200 and h.json()["status"] == "ok"
    for method, path in [("GET", "/drafts"), ("GET", "/config"), ("POST", "/generate"), ("GET", "/graph"),
                         ("GET", "/stats"), ("GET", "/audit"), ("POST", "/session"), ("POST", "/data/wipe"),
                         ("GET", "/nonexistent")]:
        r = anon.request(method, path, json={})
        assert r.status_code == 401 and r.json() == {"detail": "locked"}, (method, path)
    assert anon.get("/drafts", headers=_bearer("nope")).status_code == 401
    assert anon.get("/drafts", headers={"Authorization": TEST_APP_TOKEN}).status_code == 401  # no scheme
    assert anon.get("/drafts", headers=_bearer(TEST_APP_TOKEN)).status_code == 200


def test_host_header_checked(client):
    assert client.get("/health", headers={"host": "evil.example:8765"}).status_code == 403
    assert client.get("/drafts", headers={"host": "127.0.0.1:9999"}).status_code == 403
    assert client.get("/drafts", headers={"host": "localhost:8765"}).status_code == 200


def test_401_carries_cors_headers(anon):
    r = anon.get("/drafts", headers={"Origin": "http://localhost:1420"})
    assert r.status_code == 401 and r.headers["access-control-allow-origin"] == "http://localhost:1420"
    pre = anon.options("/drafts", headers={"Origin": "http://localhost:1420", "Access-Control-Request-Method": "GET",
                                          "Access-Control-Request-Headers": "authorization"})
    assert pre.status_code == 200


def test_token_file(runtime, client):
    p = runtime.settings.polytone_data_dir / ".api_token"
    assert p.read_text() == TEST_APP_TOKEN
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_token_from_env_or_generated(tmp_path, monkeypatch):
    from backend.security.auth import AuthManager

    monkeypatch.setenv("POLYTONE_API_TOKEN", "x" * 43)
    assert AuthManager(tmp_path, 8765).app_token == "x" * 43
    monkeypatch.delenv("POLYTONE_API_TOKEN")
    a, b = AuthManager(tmp_path / "a", 8765), AuthManager(tmp_path / "b", 8765)
    assert len(a.app_token) >= 43 and a.app_token != b.app_token
    assert (tmp_path / "a" / ".api_token").read_text() == a.app_token


# ---------------------------------------------------------------------------- sessions
def test_session_flow(client, anon):
    r = client.post("/session")
    assert r.status_code == 200
    s = r.json()
    assert s["idle_timeout_s"] == 15 * 60 and s["expires_at"] and s["token"] != TEST_APP_TOKEN
    ui = _bearer(s["token"])
    assert anon.get("/drafts", headers=ui).status_code == 200
    assert anon.get("/audit/verify", headers=ui).json()["ok"] is True
    # app-only routes refuse a session token
    assert anon.post("/session", headers=ui).status_code == 403
    assert anon.delete("/session", headers=ui).status_code == 403
    assert anon.post("/audit/event", headers=ui, json={"event": "x"}).status_code == 403
    # lock this session
    assert anon.delete("/session/current", headers=ui).json() == {"revoked": 1}
    assert anon.get("/drafts", headers=ui).json() == {"detail": "locked"}
    assert client.delete("/session/current").status_code == 400  # the app token is not a session
    # DELETE /session revokes them all
    t1, t2 = client.post("/session").json()["token"], client.post("/session").json()["token"]
    assert client.delete("/session").json() == {"revoked": 2}
    assert anon.get("/drafts", headers=_bearer(t1)).status_code == 401
    assert anon.get("/drafts", headers=_bearer(t2)).status_code == 401
    ev = _events(client)
    assert ev.count("session.unlock") == 3 and ev.count("session.lock") == 2


def test_sliding_idle_timeout_and_cap(client, anon):
    auth = client.app.state.auth
    now = [1_000_000.0]
    auth._clock = lambda: now[0]
    tok = _bearer(client.post("/session").json()["token"])
    for _ in range(3):  # activity keeps it alive past the idle window
        now[0] += 14 * 60
        assert anon.get("/stats", headers=tok).status_code == 200
    now[0] += 15 * 60  # idle
    assert anon.get("/stats", headers=tok).status_code == 401
    assert "session.expired" in _events(client)
    # absolute 12 h cap, even with constant activity
    tok = _bearer(client.post("/session").json()["token"])
    for _ in range(12 * 6):
        now[0] += 10 * 60
        anon.get("/stats", headers=tok)
    assert anon.get("/stats", headers=tok).status_code == 401
    entries = client.get("/audit?limit=1000").json()["entries"]
    assert {e["detail"].get("reason") for e in entries if e["event"] == "session.expired"} == {"idle", "max_age"}


def test_lock_idle_minutes_config(client, anon):
    assert client.put("/config", json={"lock_idle_minutes": 0}).status_code == 422
    assert client.put("/config", json={"lock_idle_minutes": 241}).status_code == 422
    assert client.put("/config", json={"retention_days": -1}).status_code == 422
    assert client.put("/config", json={"retention_days": 3651}).status_code == 422
    out = client.put("/config", json={"lock_idle_minutes": 5, "retention_days": 30}).json()
    assert out["lock_idle_minutes"] == 5 and out["retention_days"] == 30
    assert client.post("/session").json()["idle_timeout_s"] == 300
    auth = client.app.state.auth
    now = [5_000_000.0]
    auth._clock = lambda: now[0]
    tok = _bearer(client.post("/session").json()["token"])
    now[0] += 301
    assert anon.get("/config", headers=tok).status_code == 401


def test_auth_denied_audited_rate_limited(client, anon):
    for _ in range(20):
        anon.get("/drafts")
    denied = [e for e in client.get("/audit?limit=1000").json()["entries"] if e["event"] == "auth.denied"]
    assert len(denied) == 1 and denied[0]["detail"]["path"] == "/drafts"


# ---------------------------------------------------------------------------- audit API
def test_audit_endpoints_and_events(client, runtime):
    client.put("/config", json={"capture_enabled": True, "hotkey": "Alt+P"})
    client.put("/config", json={"capture_enabled": False})
    client.post("/ingest/manual", json={"text": HUMAN_SAMPLES[0]})
    client.post("/generate", json={"intent": "say thanks for the review"})
    r = client.post("/audit/event", json={"event": "session.unlock_failed", "detail": {"reason": "cancelled"}})
    assert r.status_code == 200 and r.json()["seq"] > 0
    assert client.post("/audit/event", json={"event": "Bad Event!"}).status_code == 422
    body = client.get("/audit?limit=1000").json()
    ev = [e["event"] for e in body["entries"]]
    for want in ("config.change", "capture.start", "capture.stop", "ingest", "generate", "session.unlock_failed"):
        assert want in ev, want
    by = {e["event"]: e for e in body["entries"]}
    assert by["session.unlock_failed"]["actor"] == "app"
    assert by["config.change"]["actor"] == "app"
    gen = by["generate"]["detail"]
    assert gen["chars_in"] == len("say thanks for the review") and gen["model"] == "mock" and gen["ok"] is True
    assert body["head_seq"] == max(e["seq"] for e in body["entries"])
    page = client.get(f"/audit?limit=2&before_seq={body['head_seq']}").json()["entries"]
    assert [e["seq"] for e in page] == [body["head_seq"] - 1, body["head_seq"] - 2]
    assert client.get("/audit/verify").json() == {"ok": True, "count": body["head_seq"], "first_bad_seq": None,
                                                   "reason": None}


def test_audit_never_contains_user_text(client, runtime):
    client.post("/ingest/manual", json={"text": f"honestly i think {SECRET} is fine, no rush at all."})
    client.post("/ingest/typed", json={"text": f"sent {SECRET} after inject to the team", "app": "com.apple.mail",
                                       "after_inject": True})
    client.post("/generate", json={"intent": f"tell sam about {SECRET}", "app_context": "com.apple.mail"})
    client.put("/config", json={"active_persona": "Boss"})
    assert SECRET not in json.dumps(client.get("/audit?limit=1000").json())
    raw = b""
    for suffix in ("", "-wal"):
        p = runtime.settings.polytone_data_dir / ("audit.sqlite3" + suffix)
        if p.exists():
            raw += p.read_bytes()
    assert SECRET.encode() not in raw


def test_audit_verify_reports_tampering(client, runtime):
    client.post("/generate", json={"intent": "hello there"})
    c = sqlite3.connect(runtime.audit.path)
    c.execute("DROP TRIGGER audit_log_no_update")
    c.execute("UPDATE audit_log SET actor = 'ui' WHERE seq = 1")
    c.commit()
    c.close()
    v = client.get("/audit/verify").json()
    assert v["ok"] is False and v["first_bad_seq"] == 1


# ---------------------------------------------------------------------------- wipe / retention
def test_wipe(client, runtime):
    for t in HUMAN_SAMPLES:
        assert client.post("/ingest/manual", json={"text": t}).json()["accepted"] == 1
    client.post("/generate", json={"intent": "ask about friday"})
    assert client.post("/data/wipe", json={"confirm": "yes"}).status_code == 400
    r = client.post("/data/wipe", json={"confirm": "delete everything"}).json()["deleted"]
    assert r["samples"] == 3 and r["drafts"] == 1 and r["graph_nodes"] > 0
    assert client.get("/stats").json() == {"words": 0, "structures": 0, "samples": 0, "avg_sentence_len": None}
    assert client.get("/drafts").json() == [] and client.get("/graph").json()["nodes"] == []
    assert client.get("/generate") is not None
    # learning works again afterwards (caches were reset)
    assert client.post("/ingest/manual", json={"text": HUMAN_SAMPLES[0]}).json()["accepted"] == 1
    wipes = [e for e in client.get("/audit?limit=1000").json()["entries"] if e["event"] == "data.wipe"]
    assert len(wipes) == 1 and wipes[0]["detail"]["samples"] == 3


def test_retention_purge(client, runtime):
    from backend.security import retention

    db = runtime.db
    old = "2020-01-01T00:00:00.000+00:00"
    db.insert_sample(text="typed ages ago by me", source="typed", persona="Default", created_at=old)
    db.insert_sample(text="manual ages ago by me", source="manual", persona="Default", created_at=old)
    db.insert_draft(persona="Default", intent="x", text="an old draft", created_at=old)
    assert retention.purge(runtime) == {}  # retention_days = 0 → keep forever
    client.put("/config", json={"retention_days": 30})  # applying a retention purges at once
    assert [s["text"] for s in db.list_samples()] == ["manual ages ago by me"]
    assert db.list_drafts() == []
    ev = [e for e in client.get("/audit?limit=1000").json()["entries"] if e["event"] == "data.retention_purge"]
    assert ev and ev[0]["detail"] == {"retention_days": 30, "samples": 1, "drafts": 1, "draft_edits": 0}


def test_retention_task_runs_on_start(runtime):
    from backend.security import retention

    runtime.db.set_config(retention_days=1)
    runtime.db.insert_sample(text="typed ages ago by me", source="typed", persona="Default",
                             created_at="2020-01-01T00:00:00.000+00:00")
    t = retention.RetentionTask(runtime, interval_s=3600).start()
    t.stop()
    assert runtime.db.count_samples() == 0


# ---------------------------------------------------------------------------- files / keys
def test_file_hygiene(tmp_path):
    from backend.security.files import harden

    d = tmp_path / "data"
    (d / "inbox").mkdir(parents=True)
    for name in ("polytone.sqlite3", "polytone.sqlite3-wal", "backend.log", "gmail_token.json"):
        (d / name).write_text("x")
        os.chmod(d / name, 0o644)
    os.chmod(d, 0o755)
    env = tmp_path / ".env"
    env.write_text("A=1")
    os.chmod(env, 0o644)
    assert harden(d, [env]) >= 6
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert stat.S_IMODE((d / "inbox").stat().st_mode) == 0o700
    for p in [*d.glob("*.*"), d / "backend.log", env]:
        if p.is_file():
            assert stat.S_IMODE(p.stat().st_mode) == 0o600, p.name


def test_server_hardens_data_dir(runtime, client):
    d = runtime.settings.polytone_data_dir
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    assert stat.S_IMODE((d / "audit.sqlite3").stat().st_mode) == 0o600


def test_key_created_audited(tmp_path, monkeypatch, settings):
    """First start without a key: the key goes to the (mocked) Keychain and key.created is audited."""
    import keyring

    from backend.graph.store import InMemoryGraphStore
    from backend.llm.client import MockLLM
    from backend.runtime import build_runtime
    from backend.security import crypto, keys

    store: dict = {}
    monkeypatch.setattr(keyring, "get_password", lambda s, a: store.get((s, a)))
    monkeypatch.setattr(keyring, "set_password", lambda s, a, v: store.__setitem__((s, a), v))
    monkeypatch.delenv(keys.ENV_KEY)
    crypto.reset_cache()
    try:
        rt = build_runtime(settings, db_path=tmp_path / "k.sqlite3", store=InMemoryGraphStore(), llm=MockLLM(),
                           scorer=False)
        rt.db.insert_sample(text="a sample i wrote", source="manual", persona="Default")
        assert (keys.KEYCHAIN_SERVICE, keys.KEYCHAIN_ACCOUNT) in store
        assert [e["event"] for e in rt.audit.entries()] == ["key.created"]
        rt.close()
    finally:
        monkeypatch.undo()
        crypto.reset_cache()


def test_migration_audited(tmp_path, settings):
    from backend.graph.store import InMemoryGraphStore
    from backend.llm.client import MockLLM
    from backend.runtime import build_runtime
    from backend.storage import db as dbmod

    p = tmp_path / "plain.sqlite3"
    c = sqlite3.connect(p)
    c.executescript(dbmod._SCHEMA)
    c.execute("INSERT INTO samples(created_at, source, persona, text, sha256, provenance_json) VALUES "
              "('2026-01-01', 'manual', 'Default', 'plain words here', 'abc', '{}')")
    c.commit()
    c.close()
    rt = build_runtime(settings, db_path=p, store=InMemoryGraphStore(), llm=MockLLM(), scorer=False)
    (e,) = [e for e in rt.audit.entries() if e["event"] == "migration.encrypted"]
    assert e["detail"] == {"sqlite": {"samples": 1}, "neo4j": {}}
    assert InMemoryGraphStore().migrate_encryption() == {}  # nothing at rest to migrate
    rt.close()


def test_anthropic_key_from_keychain(monkeypatch, tmp_path):
    import keyring

    from backend.config import get_settings
    from backend.security import keys

    asked = []

    def fake_get(service, account):
        asked.append((service, account))
        return "sk-from-keychain" if (service, account) == (keys.KEYCHAIN_SERVICE, keys.ANTHROPIC_ACCOUNT) else None

    monkeypatch.setattr(keyring, "get_password", fake_get)
    monkeypatch.setenv("POLYTONE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYTONE_LLM", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()
    try:
        s = get_settings()
        assert s.anthropic_api_key == "sk-from-keychain" and s.llm_backend == "anthropic"
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
        get_settings.cache_clear()
        assert get_settings().anthropic_api_key == "sk-from-env"  # env wins
        monkeypatch.setenv("POLYTONE_LLM", "mock")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        get_settings.cache_clear()
        asked.clear()
        assert get_settings().anthropic_api_key == "" and asked == []  # mock never asks the Keychain
    finally:
        get_settings.cache_clear()
