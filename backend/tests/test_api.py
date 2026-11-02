import sys
import time
import types

from .conftest import HUMAN_SAMPLES

CONSTRAINT_KEYS = {
    "avg_sentence_len", "sentence_len_p90", "top_openers", "top_transitions", "top_bigrams",
    "punctuation_rates", "structures", "lexicon", "casing", "contractions_rate", "sample_count",
}
INGEST_KEYS = {"accepted", "rejected", "reasons", "sentences", "words_merged"}


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["neo4j"] is False
    assert body["llm"] == "mock" and body["embeddings"] == "none" and body["graph_store"] == "memory"
    assert body["llm_ready"] is True


def test_health_reports_unusable_llm(tmp_path):
    """POLYTONE_LLM=anthropic without a key: /health says so, /generate answers 502."""
    from backend.config import Settings
    from backend.graph.store import InMemoryGraphStore
    from backend.llm.client import MockLLM
    from backend.runtime import build_runtime
    from backend.tests.conftest import api_client

    s = Settings(polytone_data_dir=tmp_path, polytone_llm="anthropic", anthropic_api_key="",
                 polytone_embeddings="none", polytone_watch_dir=tmp_path / "inbox")
    rt = build_runtime(s, db_path=tmp_path / "x.sqlite3", store=InMemoryGraphStore(), llm=MockLLM(), scorer=False)
    with api_client(rt) as c:
        body = c.get("/health").json()
        assert body["llm"] == "anthropic" and body["llm_ready"] is False
        r = c.post("/generate", json={"intent": "hello there friend", "persona": None, "app_context": None})
        assert r.status_code == 502 and r.json()["error"] == "llm_failed"
    rt.close()


def test_cors(client):
    r = client.options(
        "/health", headers={"Origin": "tauri://localhost", "Access-Control-Request-Method": "GET"}
    )
    assert r.headers.get("access-control-allow-origin") == "tauri://localhost"
    r = client.get("/health", headers={"Origin": "http://localhost:1420"})
    assert r.headers.get("access-control-allow-origin") == "http://localhost:1420"


def test_generate_and_drafts(client):
    r = client.post("/generate", json={"intent": "- tell sam meeting moved to 3\n- ask for slides",
                                       "persona": None, "app_context": None})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"draft_id", "text", "persona", "latency_ms", "constraints"}
    assert body["persona"] == "Default" and set(body["constraints"]) == CONSTRAINT_KEYS
    assert body["constraints"]["sample_count"] == 0
    assert "Sam" in body["text"] and isinstance(body["latency_ms"], int)
    drafts = client.get("/drafts?limit=5").json()
    assert drafts[0]["draft_id"] == body["draft_id"]
    assert set(drafts[0]) == {"draft_id", "created_at", "persona", "intent", "text", "latency_ms", "score", "outcome"}
    assert client.post("/generate", json={"intent": "  \n - "}).status_code == 422
    assert client.post("/generate", json={}).status_code == 422


def test_generate_latency(client):
    for t in HUMAN_SAMPLES:
        client.post("/ingest/manual", json={"text": t})
    client.post("/generate", json={"intent": "warmup"})
    t0 = time.perf_counter()
    r = client.post("/generate", json={"intent": "- tell sam meeting moved to 3\n- ask for slides"})
    assert r.status_code == 200
    assert (time.perf_counter() - t0) * 1000 < 500
    assert r.json()["latency_ms"] < 500


def test_ingest_manual_and_stats_and_graph(client):
    assert client.get("/stats").json() == {"words": 0, "structures": 0, "samples": 0, "avg_sentence_len": None}
    r = client.post("/ingest/manual", json={"text": HUMAN_SAMPLES[0], "persona": "Friend"})
    body = r.json()
    assert set(body) == INGEST_KEYS and body["accepted"] == 1 and body["sentences"] == 3
    assert body["words_merged"] > 0
    dup = client.post("/ingest/manual", json={"text": HUMAN_SAMPLES[0]}).json()
    assert dup["rejected"] == 1 and dup["reasons"] == ["duplicate sample"]
    assert client.post("/ingest/manual", json={"text": "   "}).json()["reasons"] == ["empty text"]
    st = client.get("/stats").json()
    assert st["samples"] == 1 and st["words"] > 0 and st["structures"] > 0 and st["avg_sentence_len"] > 0
    g = client.get("/graph?limit=20&persona=Friend").json()
    assert 0 < len(g["nodes"]) <= 20 and g["edges"]
    assert set(g["nodes"][0]) == {"id", "label", "name", "weight"}
    assert set(g["edges"][0]) == {"source", "target", "type", "weight"}
    assert client.get("/graph?persona=").status_code == 200
    gen = client.post("/generate", json={"intent": "- hi", "persona": "Friend"}).json()
    assert gen["constraints"]["sample_count"] == 1


def test_firewall_rejects_resubmitted_draft(client, runtime):
    client.post("/ingest/manual", json={"text": HUMAN_SAMPLES[1]})
    words_before = dict(runtime.store.words)
    draft = client.post("/generate", json={"intent": "- tell sam meeting moved to 3\n- ask for slides"}).json()
    r = client.post("/ingest/manual", json={"text": draft["text"]}).json()
    assert r["accepted"] == 0 and r["rejected"] == 1 and "draft" in r["reasons"][0]
    r = client.post("/ingest/manual", json={"text": draft["text"] + " thanks"}).json()
    assert r["rejected"] == 1 and "draft" in r["reasons"][0]
    assert dict(runtime.store.words) == words_before
    assert client.get("/stats").json()["samples"] == 1


def test_sources_list_and_run(client, monkeypatch):
    srcs = client.get("/ingest/sources").json()
    assert [s["name"] for s in srcs] == ["manual", "gmail", "wispr", "watcher", "typed"]
    for s in srcs:
        assert set(s) == {"name", "enabled", "status", "last_run", "items_ingested"}
    assert srcs[0]["enabled"] is True
    assert client.post("/ingest/sources/nope/run").status_code == 404
    r = client.post("/ingest/sources/manual/run").json()
    assert set(r) == INGEST_KEYS and r["accepted"] == 0

    fake = types.ModuleType("backend.mcp_servers.wispr")
    fake.collect = lambda settings: [
        {"text": "yo i'm on it. back in five.", "provenance": {"generated": False, "raw_transcript": True, "app": "com.tinyspeck.slackmacgap"}},
        {"text": "AI wrote this one", "provenance": {"generated": True, "raw_transcript": True}},
        {"text": "raw dictation works too.", "provenance": {"text_kind": "raw", "app": "com.apple.Notes"}},
        "a bare string has no raw-transcript provenance.",
    ]
    monkeypatch.setitem(sys.modules, "backend.mcp_servers.wispr", fake)
    r = client.post("/ingest/sources/wispr/run").json()
    assert r["accepted"] == 2 and r["rejected"] == 2
    assert r["reasons"][0] == "flagged as generated" and "raw transcript" in r["reasons"][1]
    w = {s["name"]: s for s in client.get("/ingest/sources").json()}["wispr"]
    assert w["enabled"] is True and w["items_ingested"] == 2 and w["last_run"]

    broken = types.ModuleType("backend.mcp_servers.file_watcher")

    def boom(settings):
        raise RuntimeError("disk gone")
        yield  # pragma: no cover

    broken.collect = boom
    monkeypatch.setitem(sys.modules, "backend.mcp_servers.file_watcher", broken)
    r = client.post("/ingest/sources/watcher/run").json()
    assert r["accepted"] == 0 and "disk gone" in r["reasons"][0]
    w = {s["name"]: s for s in client.get("/ingest/sources").json()}["watcher"]
    assert w["status"] == "error"


def test_personas(client):
    names = [p["name"] for p in client.get("/personas").json()]
    assert {"Boss", "Friend", "Investor", "Default"} <= set(names)
    client.post("/ingest/manual", json={"text": "custom persona words.", "persona": "Mom"})
    assert "Mom" in [p["name"] for p in client.get("/personas").json()]


def test_config(client):
    from backend.storage.db import DEFAULT_CAPTURE_APPS

    cap = {"capture_enabled": False, "capture_apps": DEFAULT_CAPTURE_APPS, "lock_idle_minutes": 15, "retention_days": 0,
           "learning_eval_days": 7,
           "draft_speed": "quality"}
    assert client.get("/config").json() == {
        "active_persona": "Default", "hotkey": "CmdOrCtrl+Shift+P", "inject_mode": "paste", **cap}
    r = client.put("/config", json={"active_persona": "Boss", "hotkey": "Alt+P", "inject_mode": "type"})
    assert r.json() == {"active_persona": "Boss", "hotkey": "Alt+P", "inject_mode": "type", **cap}
    assert client.put("/config", json={"inject_mode": "telepathy"}).status_code == 422
    assert client.put("/config", json={"inject_mode": "paste"}).json()["active_persona"] == "Boss"
    assert client.post("/generate", json={"intent": "hi"}).json()["persona"] == "Boss"


def test_scores_summary(client, runtime):
    assert client.get("/scores/summary").json() == {"mean": None, "count": 0, "trend": []}
    runtime.scorer = lambda text, base, persona=None: {"score": 60.0, "distance": 0.4, "components": {}}
    client.post("/ingest/manual", json={"text": HUMAN_SAMPLES[2]})
    client.post("/generate", json={"intent": "a"})
    runtime.scorer = lambda text, base, persona=None: {"score": 80.0, "distance": 0.2, "components": {}}
    client.post("/generate", json={"intent": "b"})
    s = client.get("/scores/summary").json()
    assert s["count"] == 2 and s["mean"] == 70.0 and [x["score"] for x in s["trend"]] == [60.0, 80.0]
    assert client.get("/drafts").json()[0]["score"]["score"] == 80.0


def test_warmup_is_background_and_optional(runtime, monkeypatch):
    from backend.daemon import server
    from backend.evaluation import embeddings
    from backend.tests.conftest import api_client

    started = []

    def slow_warm():
        started.append(1)
        time.sleep(0.5)
        return True

    monkeypatch.setattr(embeddings, "warm_up", slow_warm)
    server._warm_embeddings(runtime)  # POLYTONE_EMBEDDINGS=none → nothing loaded
    assert started == []
    runtime.settings.polytone_embeddings = "qwen"
    t0 = time.perf_counter()
    with api_client(runtime) as c:
        assert c.get("/health").status_code == 200
        assert time.perf_counter() - t0 < 0.4  # /health never waits for the embedding model
        warm = c.app.state.warmup
        assert warm.daemon
        warm.join(3)
    assert started == [1] and "generate" in runtime._graphs  # graph compiled during warm-up


def test_env_file_switches_reach_os_environ(tmp_path, monkeypatch):
    import os

    from backend.config import export_env_file

    env = tmp_path / ".env"
    env.write_text("POLYTONE_USER_EMAILS=me@example.com\nPOLYTONE_WATCHER=0\nANTHROPIC_API_KEY=sk-x\n")
    monkeypatch.delenv("POLYTONE_USER_EMAILS", raising=False)
    monkeypatch.setenv("POLYTONE_WATCHER", "1")  # real env wins over .env
    export_env_file(env)
    assert os.environ["POLYTONE_USER_EMAILS"] == "me@example.com"
    assert os.environ["POLYTONE_WATCHER"] == "1"
    assert os.environ.get("ANTHROPIC_API_KEY") == ""  # secrets stay in Settings only
