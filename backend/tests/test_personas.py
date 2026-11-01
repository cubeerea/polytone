"""User-managed personas: create / reword / reset / delete, the tone line in the prompt, and
deleting a persona moving its samples and graph learning to Default."""
from __future__ import annotations

import pytest

from backend import personas
from backend.graph.store import InMemoryGraphStore
from backend.ingestion.extract import extract
from backend.langgraph.generate_graph import run_generation
from backend.langgraph.ingest_graph import run_ingest
from backend.langgraph.prompts import PERSONAS as BUILTIN

from .conftest import HUMAN_SAMPLES, api_client

RECRUITER = "warm, brief, always ends with a clear next step"


def names(db):
    return [p["name"] for p in personas.list_personas(db)]


def system_prompt(runtime, persona):
    run_generation("tell sam the meeting moved to 3", persona, runtime=runtime)
    return runtime.llm.calls[-1][0]


# ---------------------------------------------------------------- listing
def test_builtins_are_listed_first_and_flagged(runtime):
    ps = personas.list_personas(runtime.db)
    assert [p["name"] for p in ps] == list(BUILTIN)
    assert all(p["builtin"] and not p["edited"] and p["samples"] == 0 for p in ps)
    assert ps[1]["description"] == BUILTIN["Boss"]


def test_samples_learned_under_an_unmanaged_name_show_up_as_custom(runtime):
    run_ingest(HUMAN_SAMPLES[0], "manual", "Coach", {"source": "manual"}, runtime=runtime)
    coach = personas.find(runtime.db, "coach")
    assert coach and not coach["builtin"] and not coach["edited"] and coach["samples"] == 1


# ---------------------------------------------------------------- create / validate
def test_create_a_persona_and_its_tone_reaches_the_prompt(runtime):
    p = personas.create(runtime.db, "  Recruiter ", RECRUITER)
    assert p == {"name": "Recruiter", "description": RECRUITER, "builtin": False, "edited": True, "samples": 0}
    assert names(runtime.db)[-1] == "Recruiter"
    assert f"PERSONA: Recruiter. {RECRUITER}" in system_prompt(runtime, "Recruiter")


@pytest.mark.parametrize("name, desc, status", [
    ("", RECRUITER, 400),
    ("x" * 31, RECRUITER, 400),
    ("<script>", RECRUITER, 400),
    ("Boss!", RECRUITER, 400),
    ("Recruiter", "", 400),
    ("Recruiter", "y" * 301, 400),
    ("boss", RECRUITER, 409),  # a built-in, any case
])
def test_create_rejects_bad_input(runtime, name, desc, status):
    with pytest.raises(personas.PersonaError) as e:
        personas.create(runtime.db, name, desc)
    assert e.value.status == status


def test_names_allow_ordinary_punctuation_and_unicode(runtime):
    for n in ("Mom & Dad", "Co-founder", "VC (seed)", "Équipe", "C++"):
        assert personas.create(runtime.db, n, RECRUITER)["name"] == n


def test_duplicates_are_case_insensitive(runtime):
    personas.create(runtime.db, "Recruiter", RECRUITER)
    with pytest.raises(personas.PersonaError) as e:
        personas.create(runtime.db, "RECRUITER", "other")
    assert e.value.status == 409


def test_custom_persona_limit(runtime):
    for i in range(personas.MAX_CUSTOM):
        personas.create(runtime.db, f"P{i}", RECRUITER)
    with pytest.raises(personas.PersonaError, match="up to"):
        personas.create(runtime.db, "One more", RECRUITER)


def test_creating_an_unmanaged_name_adopts_its_samples(runtime):
    run_ingest(HUMAN_SAMPLES[0], "manual", "Coach", {"source": "manual"}, runtime=runtime)
    p = personas.create(runtime.db, "coach", "encouraging and direct")
    assert p["name"] == "Coach" and p["samples"] == 1 and p["edited"]
    assert names(runtime.db).count("Coach") == 1


def test_description_is_encrypted_at_rest(runtime):
    personas.create(runtime.db, "Recruiter", RECRUITER)
    with runtime.db.connect() as c:
        (raw,) = c.execute("SELECT description FROM personas").fetchone()
    assert raw.startswith("enc1:") and "warm" not in raw


# ---------------------------------------------------------------- reword / reset
def test_reword_and_reset_a_builtin(runtime):
    mine = "short, no pleasantries, numbers first"
    p = personas.update(runtime.db, "boss", mine)
    assert p["name"] == "Boss" and p["description"] == mine and p["edited"] and p["builtin"]
    assert f"PERSONA: Boss. {mine}" in system_prompt(runtime, "Boss")
    p = personas.update(runtime.db, "Boss", None)
    assert p["description"] == BUILTIN["Boss"] and not p["edited"]
    assert f"PERSONA: Boss. {BUILTIN['Boss']}" in system_prompt(runtime, "Boss")


def test_reword_a_custom_persona(runtime):
    personas.create(runtime.db, "Recruiter", RECRUITER)
    assert personas.update(runtime.db, "Recruiter", "formal")["description"] == "formal"
    with pytest.raises(personas.PersonaError):
        personas.update(runtime.db, "Recruiter", None)  # only built-ins reset
    with pytest.raises(personas.PersonaError) as e:
        personas.update(runtime.db, "Nobody", "x")
    assert e.value.status == 404


def test_unknown_persona_still_gets_a_generic_tone_line(runtime):
    assert "'Landlord' persona" in system_prompt(runtime, "Landlord")


# ---------------------------------------------------------------- delete
def test_builtins_cannot_be_deleted(runtime):
    for n in BUILTIN:
        with pytest.raises(personas.PersonaError, match="built in"):
            personas.delete(runtime, n)
    with pytest.raises(personas.PersonaError) as e:
        personas.delete(runtime, "Nobody")
    assert e.value.status == 404


def test_delete_moves_samples_and_learning_to_default(runtime):
    personas.create(runtime.db, "Recruiter", RECRUITER)
    for t in HUMAN_SAMPLES[:2]:
        run_ingest(t, "manual", "Recruiter", {"source": "manual"}, runtime=runtime)
    run_ingest(HUMAN_SAMPLES[2], "manual", "Default", {"source": "manual"}, runtime=runtime)
    runtime.db.set_config(active_persona="Recruiter")
    before_global = runtime.store.get_constraints(None)
    out = personas.delete(runtime, "recruiter")
    assert out == {"name": "Recruiter", "moved_samples": 2, "active_persona": "Default"}
    assert "Recruiter" not in names(runtime.db)
    assert runtime.db.get_config()["active_persona"] == "Default"
    assert runtime.db.count_samples_by_persona() == {"Default": 3}
    assert runtime.store.get_constraints("Default")["sample_count"] == 3
    assert runtime.store.get_constraints(None) == before_global  # nothing learned was lost


def test_delete_keeps_another_active_persona(runtime):
    personas.create(runtime.db, "Recruiter", RECRUITER)
    runtime.db.set_config(active_persona="Boss")
    assert personas.delete(runtime, "Recruiter")["active_persona"] == "Boss"


def test_graph_failure_on_delete_changes_nothing(runtime, monkeypatch):
    personas.create(runtime.db, "Recruiter", RECRUITER)
    run_ingest(HUMAN_SAMPLES[0], "manual", "Recruiter", {"source": "manual"}, runtime=runtime)

    def down(*_a):
        raise ConnectionError("neo4j down")

    monkeypatch.setattr(runtime.store, "merge_persona", down)
    runtime.db.set_config(active_persona="Recruiter")
    with pytest.raises(personas.GraphUnavailable) as e:
        personas.delete(runtime, "Recruiter")
    assert e.value.status == 503
    assert personas.find(runtime.db, "Recruiter")["samples"] == 1
    assert runtime.db.get_config()["active_persona"] == "Recruiter"  # restored


def test_delete_refuses_while_running_on_the_in_memory_fallback(runtime):
    personas.create(runtime.db, "Recruiter", RECRUITER)
    runtime.store.fallback = True  # Neo4j was down at start-up
    with pytest.raises(personas.GraphUnavailable):
        personas.delete(runtime, "Recruiter")
    assert personas.find(runtime.db, "Recruiter") is not None


def test_memory_store_merge_equals_learning_under_default_directly():
    texts = HUMAN_SAMPLES + ["ok cool, see you at 3 — bring the notes!"]
    merged, direct = InMemoryGraphStore(), InMemoryGraphStore()
    for i, t in enumerate(texts):
        merged.write_extraction(extract(t), "X" if i % 2 else "Default")
        direct.write_extraction(extract(t), "Default")
    assert merged.merge_persona("X", "Default") == 2
    assert merged._aggregate("Default", 20) == direct._aggregate("Default", 20)
    assert merged._aggregate(None, 20) == direct._aggregate(None, 20)
    assert merged._aggregate("X", 20).sample_count == 0
    assert merged.merge_persona("X", "Default") == 0  # already gone


def test_merge_into_a_persona_that_has_nothing_yet():
    s = InMemoryGraphStore()
    s.write_extraction(extract(HUMAN_SAMPLES[0]), "X")
    ref = s._aggregate("X", 20)
    assert s.merge_persona("X", "Default") == 1
    assert s._aggregate("Default", 20) == ref


# ---------------------------------------------------------------- API
def test_api_round_trip(client, runtime):
    assert [p["name"] for p in client.get("/personas").json()] == list(BUILTIN)
    r = client.post("/personas", json={"name": "Recruiter", "description": RECRUITER})
    assert r.status_code == 200 and r.json()["name"] == "Recruiter"
    assert client.post("/personas", json={"name": "recruiter", "description": "x"}).status_code == 409
    assert client.post("/personas", json={"name": "", "description": "x"}).status_code == 400
    assert client.put("/personas/Recruiter", json={"description": "formal"}).json()["description"] == "formal"
    assert client.put("/personas/Boss", json={"description": None}).json()["edited"] is False
    assert client.delete("/personas/Boss").status_code == 400
    assert client.delete("/personas/Nobody").status_code == 404
    r = client.delete("/personas/Recruiter")
    assert r.status_code == 200 and r.json()["moved_samples"] == 0
    events = [e["event"] for e in runtime.audit.entries(20)]
    assert {"persona.create", "persona.update", "persona.delete"} <= set(events)
    assert not any(RECRUITER in str(e) for e in runtime.audit.entries(20))  # tone text never audited


def test_api_needs_the_token(runtime):
    with api_client(runtime, token=None) as anon:
        assert anon.post("/personas", json={"name": "A", "description": "b"}).status_code == 401
        assert anon.delete("/personas/A").status_code == 401


def test_api_delete_reports_a_graph_outage_as_503(client, runtime, monkeypatch):
    client.post("/personas", json={"name": "Recruiter", "description": RECRUITER})
    monkeypatch.setattr(runtime.store, "merge_persona", lambda *a: (_ for _ in ()).throw(ConnectionError()))
    r = client.delete("/personas/Recruiter")
    assert r.status_code == 503 and "nothing was deleted" in r.json()["detail"]
    assert "Recruiter" in names(runtime.db)


# ---------------------------------------------------------------- names in other entry points
def test_ingest_and_generate_use_the_stored_spelling(runtime):
    personas.create(runtime.db, "Recruiter", RECRUITER)
    run_ingest(HUMAN_SAMPLES[0], "manual", "recruiter", {"source": "manual"}, runtime=runtime)
    run_ingest(HUMAN_SAMPLES[1], "manual", "boss", {"source": "manual"}, runtime=runtime)
    assert runtime.db.count_samples_by_persona() == {"Recruiter": 1, "Boss": 1}
    assert run_generation("say hi", "RECRUITER", runtime=runtime)["persona"] == "Recruiter"
    assert f"PERSONA: Recruiter. {RECRUITER}" in runtime.llm.calls[-1][0]


def test_config_only_accepts_an_existing_persona(client):
    client.post("/personas", json={"name": "Recruiter", "description": RECRUITER})
    assert client.put("/config", json={"active_persona": "recruiter"}).json()["active_persona"] == "Recruiter"
    assert client.put("/config", json={"active_persona": "boss"}).json()["active_persona"] == "Boss"
    assert client.put("/config", json={"active_persona": "Ghost"}).status_code == 422


def test_ingest_waits_for_a_running_delete(runtime, monkeypatch):
    """A sample for the persona being deleted is written either before the move (and moved) or
    after it (under the persona name, which then shows as a sample-only persona) — never half."""
    import threading

    personas.create(runtime.db, "Recruiter", RECRUITER)
    started, release = threading.Event(), threading.Event()
    real = runtime.store.merge_persona

    def slow_merge(src, dst):
        started.set()
        release.wait(5)
        return real(src, dst)

    monkeypatch.setattr(runtime.store, "merge_persona", slow_merge)
    t = threading.Thread(target=personas.delete, args=(runtime, "Recruiter"))
    t.start()
    assert started.wait(5)
    done = threading.Event()
    w = threading.Thread(target=lambda: (run_ingest(HUMAN_SAMPLES[0], "manual", "Recruiter",
                                                    {"source": "manual"}, runtime=runtime), done.set()))
    w.start()
    assert not done.wait(0.3)  # blocked behind the delete
    release.set()
    t.join(5)
    w.join(5)
    assert done.is_set()
    # written after the delete: SQLite and the graph agree on where it went
    assert runtime.db.count_samples_by_persona() == {"Recruiter": 1}
    assert runtime.store.get_constraints("Recruiter")["sample_count"] == 1


def test_merge_drops_a_key_whose_ciphertext_is_missing():
    from backend.graph.store import merged_context

    out = merged_context({}, {"opener_keys": ["k1", "k2"], "opener_counts": [1, 2], "opener_texts": ["e1"]},
                         texts=True)
    assert out["opener_keys"] == ["k1"] and out["opener_texts"] == ["e1"] and out["opener_counts"] == [1]
