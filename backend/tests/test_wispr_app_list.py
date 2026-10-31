"""Wispr Flow dictation follows the same Learning app list as typing capture (``capture_apps``).

A dictation is learned only when the app Wispr recorded for it is in the list; a dictation with
no recorded app is never learned. The rule is enforced in the ingestion firewall (every Wispr
path: local history, the live watcher, the WebSocket, MCP tools) and counted early by the local
history reader so the UI can say how many were skipped."""
from __future__ import annotations

import json

import pytest

from backend.ingestion import app_policy, sources
from backend.ingestion.firewall import check
from backend.langgraph.ingest_graph import run_ingest
from backend.mcp_servers import wispr, wispr_live
from backend.tests.test_wispr_live import SLACK, FakeWispr

CHROME = "com.google.Chrome"
VSCODE = "com.microsoft.VSCode"
TEXT = "honestly i think we should just ship the smaller version first and see"


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


def set_apps(runtime, apps):
    runtime.db.set_config(capture_apps=json.dumps(apps))


# ---------------------------------------------------------------- the matching rule
def test_policy_matches_bundle_ids_and_old_display_names():
    apps = [SLACK, "com.apple.Notes"]
    assert app_policy.skip_reason(SLACK, apps) is None
    assert app_policy.skip_reason(SLACK.upper(), apps) is None  # case-insensitive
    assert app_policy.skip_reason("Slack", apps) is None  # older Wispr versions stored names
    assert app_policy.skip_reason("notes", apps) is None
    assert app_policy.skip_reason(CHROME, apps) == app_policy.NOT_LISTED
    assert app_policy.skip_reason("Google Chrome", apps) == app_policy.NOT_LISTED
    assert app_policy.skip_reason("", apps) == app_policy.NO_APP
    assert app_policy.skip_reason(None, apps) == app_policy.NO_APP
    assert app_policy.skip_reason(SLACK, []) == app_policy.NOT_LISTED


def test_allowed_apps_fails_closed(runtime):
    class Broken:
        def get_config(self):
            raise RuntimeError("db gone")

    assert app_policy.allowed_apps(Broken()) == []
    assert app_policy.allowed_apps(object()) == []
    # a corrupt stored list must not fall back to the (browser-including) default list
    for bad in ("not json", '{"a": 1}', '"com.google.Chrome"', "null"):
        runtime.db.set_config(capture_apps=bad)
        assert app_policy.allowed_apps(runtime.db) == [], bad
        ok, why = check(TEXT, "wispr", {"raw_transcript": True, "app": CHROME}, runtime.db)
        assert not ok and "Learning app list" in why
    set_apps(runtime, [" com.apple.Notes ", "", 7])
    assert app_policy.allowed_apps(runtime.db) == ["com.apple.Notes"]


def test_generic_names_do_not_alias_unrelated_apps():
    assert app_policy.skip_reason("Code", ["com.microsoft.VSCode"]) == app_policy.NOT_LISTED
    assert app_policy.skip_reason("Claude", ["com.anthropic.claudefordesktop"]) == app_policy.NOT_LISTED


def test_insertion_index_matches_old_display_names_to_bundle_ids():
    assert wispr_live._same_app("Slack", SLACK) and wispr_live._same_app(SLACK, "slack")
    assert wispr_live._same_app(None, SLACK)
    assert not wispr_live._same_app("Slack", CHROME)


# ---------------------------------------------------------------- firewall (every Wispr path)
def test_firewall_rejects_wispr_from_unlisted_or_unknown_apps(runtime):
    set_apps(runtime, [SLACK])
    db = runtime.db
    assert check(TEXT, "wispr", {"raw_transcript": True, "app": SLACK}, db) == (True, None)
    ok, why = check(TEXT, "wispr", {"raw_transcript": True, "app": CHROME}, db)
    assert not ok and "not in the Learning app list" in why
    ok, why = check(TEXT, "wispr", {"raw_transcript": True}, db)
    assert not ok and "no recorded app" in why
    # other sources are untouched by the list
    assert check(TEXT, "manual", {}, db) == (True, None)


def test_run_ingest_never_stores_an_unlisted_dictation(runtime):
    set_apps(runtime, [SLACK])
    r = run_ingest(TEXT, "wispr", None, {"raw_transcript": True, "app": CHROME}, runtime=runtime)
    assert r["accepted"] == 0 and "Learning app list" in r["reasons"][0]
    assert runtime.db.count_samples(source="wispr") == 0


# ---------------------------------------------------------------- local history (on demand)
def test_local_history_learns_only_listed_apps(fake, runtime):
    set_apps(runtime, [SLACK, VSCODE])
    fake.add("ok so the slack one should be learned from now", app=SLACK)
    fake.add("this went into chrome and must stay private", app=CHROME)
    fake.add("this one has no app recorded at all", app=None)
    fake.add("and the vs code dictation is fine to learn", app=VSCODE)
    items, info = wispr.collect_local(runtime.settings, db=runtime.db)
    assert sorted(i["provenance"]["app"] for i in items) == sorted([SLACK, VSCODE])
    assert info["skipped"] == {app_policy.NOT_LISTED: 1, app_policy.NO_APP: 1}

    r = sources.run_source("wispr", runtime)  # end to end: nothing new, nothing from Chrome
    texts = [s["text"] for s in runtime.db.list_samples(source="wispr")]
    assert all("chrome" not in t and "no app" not in t for t in texts), (r, texts)


def test_list_change_applies_to_the_next_dictation_without_restart(fake, runtime):
    set_apps(runtime, [SLACK])
    fake.add("first chrome dictation before chrome was allowed", app=CHROME)
    assert wispr.collect_local(runtime.settings, db=runtime.db)[0] == []
    set_apps(runtime, [SLACK, CHROME])
    fake.add("second chrome dictation after chrome was allowed", app=CHROME)
    items, _ = wispr.collect_local(runtime.settings, db=runtime.db)
    # adding an app is not retroactive (same as typing): only the new dictation is learned
    assert [i["text"] for i in items] == ["second chrome dictation after chrome was allowed"]
    set_apps(runtime, [SLACK])  # removing it takes effect immediately too
    fake.add("third chrome dictation after chrome was removed", app=CHROME)
    assert wispr.collect_local(runtime.settings, db=runtime.db)[0] == []


def test_config_api_drives_the_wispr_rule(fake, runtime, monkeypatch):
    from backend.tests.conftest import api_client

    monkeypatch.setenv("POLYTONE_WISPR_LIVE", "0")  # the live watcher would race the explicit run
    with api_client(runtime) as client:
        _config_api_case(fake, runtime, client)


def _config_api_case(fake, runtime, client):
    assert client.put("/config", json={"capture_apps": [VSCODE]}).status_code == 200
    fake.add("dictated into slack which is no longer in the list", app=SLACK)
    fake.add("dictated into vs code which the user allowed", app=VSCODE)
    r = client.post("/ingest/sources/wispr/run").json()
    assert r["accepted"] == 1, r
    assert [s["provenance"]["app"] for s in runtime.db.list_samples(source="wispr")] == [VSCODE]


# ---------------------------------------------------------------- live watcher
def test_live_watcher_counts_and_reports_skipped_dictations(fake, runtime, monkeypatch):
    set_apps(runtime, [SLACK])
    fake.add("live slack dictation that should be learned", app=SLACK)
    fake.add("live chrome dictation that should be skipped", app=CHROME)
    fake.add("live dictation with no app that should be skipped", app=None)
    t = wispr_live.WisprLiveWatcher(runtime, fake.path)
    t.tick()
    assert t.stats["accepted"] == 1 and t.stats["skipped_app"] == 2
    assert [s["provenance"]["app"] for s in runtime.db.list_samples(source="wispr")] == [SLACK]
    monkeypatch.setattr(wispr_live, "active", lambda: t)
    assert "2 skipped (app not in your Learning list)" in wispr_live.status_text()


def test_skipped_dictations_still_feed_the_typed_insertion_index(fake, runtime):
    """Wispr text pasted into an unlisted app is never learned, but the index that keeps Wispr's
    AI-formatted output out of typed samples must still see it."""
    set_apps(runtime, [SLACK])
    fake.add("raw words spoken into chrome", "Polished words spoken into Chrome.", app=CHROME)
    t = wispr_live.WisprLiveWatcher(runtime, fake.path)
    t.tick()
    assert runtime.db.count_samples(source="wispr") == 0
    assert wispr_live.INSERTIONS.candidates(CHROME)


# ---------------------------------------------------------------- WebSocket messages
@pytest.mark.parametrize("msg, app", [
    ({"type": "final", "raw_text": TEXT, "app": SLACK}, SLACK),
    ({"type": "final", "data": {"asrText": TEXT, "bundleId": VSCODE}}, VSCODE),
    ({"type": "final", "app": CHROME, "data": {"asrText": TEXT}}, CHROME),  # app on the envelope
    ({"type": "final", "raw_text": TEXT}, None),
])
def test_ws_messages_carry_their_app(msg, app):
    parsed = wispr.parse_message(json.dumps(msg))
    assert parsed and parsed["app"] == app


def test_ws_dictation_without_listed_app_is_rejected(runtime):
    set_apps(runtime, [SLACK])
    for msg, accepted in (({"raw_text": TEXT, "app": SLACK}, 1), ({"raw_text": TEXT + " again", "app": CHROME}, 0),
                          ({"raw_text": TEXT + " once more"}, 0)):
        item = wispr._ws_item(wispr.parse_message(msg))
        r = run_ingest(item["text"], "wispr", None, item["provenance"], runtime=runtime)
        assert r["accepted"] == accepted, (msg, r)
