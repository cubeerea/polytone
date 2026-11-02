"""Typing capture (source "typed"): /config fields, POST /ingest/typed, draft_edits, sources."""
from __future__ import annotations

from backend.ingestion import firewall
from backend.storage.db import DEFAULT_CAPTURE_APPS, Database

from .conftest import HUMAN_SAMPLES

SLACK = "com.tinyspeck.slackmacgap"


def _enable(client, apps=None):
    body = {"capture_enabled": True}
    if apps is not None:
        body["capture_apps"] = apps
    r = client.put("/config", json=body)
    assert r.status_code == 200
    return r.json()


def test_capture_config_defaults_and_persist(db: Database):
    assert db.capture_config() == (False, DEFAULT_CAPTURE_APPS)
    db.set_config(capture_enabled=True, capture_apps=["com.apple.mail"])
    assert Database(db.path).capture_config() == (True, ["com.apple.mail"])  # persisted
    db.set_config(capture_enabled=False)
    assert db.capture_config() == (False, ["com.apple.mail"])


def test_config_capture_roundtrip(client):
    cfg = client.get("/config").json()
    assert cfg["capture_enabled"] is False and cfg["capture_apps"] == DEFAULT_CAPTURE_APPS
    out = _enable(client, [" com.apple.mail ", "com.apple.mail", "", SLACK])
    assert out["capture_enabled"] is True and out["capture_apps"] == ["com.apple.mail", SLACK]
    # unrelated PUTs leave capture settings alone; the list may be emptied explicitly
    assert client.put("/config", json={"inject_mode": "type"}).json()["capture_apps"] == ["com.apple.mail", SLACK]
    assert client.put("/config", json={"capture_apps": []}).json()["capture_apps"] == []
    assert client.get("/config").json()["capture_enabled"] is True
    assert client.put("/config", json={"capture_enabled": "maybe"}).status_code == 422


def test_typed_accepted(client, runtime):
    _enable(client)
    r = client.post("/ingest/typed", json={"text": HUMAN_SAMPLES[0], "app": SLACK, "app_name": "Slack",
                                           "after_inject": False})
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] == 1 and body["rejected"] == 0 and body["sentences"] >= 1
    [s] = runtime.db.list_samples(source="typed")
    assert s["persona"] == "Default"
    assert s["provenance"]["typed"] is True and s["provenance"]["app"] == SLACK
    assert s["provenance"]["app_name"] == "Slack"
    # resending the same message is a duplicate
    again = client.post("/ingest/typed", json={"text": HUMAN_SAMPLES[0], "app": SLACK}).json()
    assert again["accepted"] == 0 and again["reasons"] == ["duplicate sample"]


def test_typed_ignores_active_persona(client, runtime):
    _enable(client)
    client.put("/config", json={"active_persona": "Boss"})
    client.post("/ingest/typed", json={"text": HUMAN_SAMPLES[1], "app": SLACK})
    assert runtime.db.list_samples(source="typed")[0]["persona"] == "Default"


def test_typed_rejected_when_capture_disabled(client, runtime):
    r = client.post("/ingest/typed", json={"text": HUMAN_SAMPLES[0], "app": SLACK}).json()
    assert r["accepted"] == 0 and r["rejected"] == 1 and "disabled" in r["reasons"][0]
    assert runtime.db.count_samples(source="typed") == 0


def test_typed_rejected_when_it_matches_a_draft(client, runtime):
    _enable(client)
    draft = client.post("/generate", json={"intent": "- tell sam meeting moved to 3\n- ask for slides"}).json()
    r = client.post("/ingest/typed", json={"text": draft["text"], "app": SLACK}).json()
    assert r["accepted"] == 0 and r["rejected"] == 1 and "Polytone-generated draft" in r["reasons"][0]
    assert runtime.db.count_samples(source="typed") == 0


def test_typed_after_inject_goes_to_draft_edits_only(client, runtime):
    _enable(client)
    draft = client.post("/generate", json={"intent": "- tell sam meeting moved to 3"}).json()
    words_before = dict(runtime.store.words)
    text = "totally fresh words the user typed after editing the draft, thanks!"
    r = client.post("/ingest/typed", json={"text": text, "app": SLACK, "app_name": "Slack", "after_inject": True})
    assert r.status_code == 200
    assert r.json() == {"accepted": 0, "rejected": 1, "sentences": 0, "words_merged": 0,
                        "reasons": ["after_polytone_inject: stored for edit learning"]}
    [edit] = runtime.db.list_draft_edits()
    # unrelated to the draft (no shared shingles): stored, but not linked and nothing learned
    assert edit["text"] == text and edit["app"] == SLACK and edit["nearest_draft_id"] is None
    assert runtime.db.get_corrections("Default") == []
    assert runtime.db.count_samples() == 0 and dict(runtime.store.words) == words_before
    assert draft["draft_id"]


def test_after_inject_without_recent_draft(runtime):
    from backend.ingestion.sources import ingest_typed

    old = "2020-01-01T00:00:00.000+00:00"
    runtime.db.insert_draft(persona="Default", intent="x", text="an old draft from long ago", created_at=old)
    r = ingest_typed(runtime, "some edited text here", SLACK, after_inject=True)
    assert r["rejected"] == 1  # stored even while capture_enabled is False
    assert runtime.db.list_draft_edits()[0]["nearest_draft_id"] is None


def test_typed_text_capped(client, runtime):
    _enable(client)
    long = " ".join(f"word{i} goes here." for i in range(2000))
    assert client.post("/ingest/typed", json={"text": long, "app": SLACK}).json()["accepted"] == 1
    assert len(runtime.db.list_samples(source="typed")[0]["text"]) <= 5000


def test_firewall_typed_provenance(db: Database):
    text = "this is a perfectly human sentence i typed."
    assert firewall.check(text, "typed", {"typed": True, "app": SLACK}, db) == (True, None)
    ok, reason = firewall.check(text, "typed", {"app": SLACK}, db)
    assert not ok and "typed=True" in reason
    ok, reason = firewall.check(text, "typed", {"typed": True, "after_inject": True}, db)
    assert not ok and "after_inject" in reason


def test_typed_source_listing_and_run(client):
    t = {s["name"]: s for s in client.get("/ingest/sources").json()}["typed"]
    assert t == {"name": "typed", "enabled": False, "status": "disabled", "last_run": None, "items_ingested": 0}
    _enable(client)
    client.post("/ingest/typed", json={"text": HUMAN_SAMPLES[2], "app": SLACK})
    t = {s["name"]: s for s in client.get("/ingest/sources").json()}["typed"]
    assert t["enabled"] is True and t["status"] == "listening" and t["items_ingested"] == 1
    assert t["last_run"] is not None
    r = client.post("/ingest/sources/typed/run")
    assert r.status_code == 400 and "push-only" in r.json()["detail"]
