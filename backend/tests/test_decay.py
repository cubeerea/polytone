"""Recency weighting of the style graph (graph/decay.py) — in-memory store, fake clock.

Neo4j versions of the parity / rebase / crash / prune checks live in ``test_neo4j.py``.
"""
from __future__ import annotations

import math

import pytest

from backend.graph import decay as D
from backend.graph.constraints import blend_weight
from backend.graph.store import InMemoryGraphStore
from backend.ingestion.extract import extract

DAY = D.DAY_S
H = 10.0  # half-life (days) used by most tests
T0 = 1_750_000_000.0


class Clock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, days: float) -> None:
        self.t += days * DAY


# Style A: lower case, short, emoji, "lol" opener. Style B: sentence case, long, semicolons.
STYLE_A = [
    "lol ok see u at the park. lol that was fun 😂 lol same time tmrw?",
    "lol no way. lol u serious 😂 lol fine.",
    "lol ok. lol brb 😂 lol later dude.",
    "lol yes. lol bring snacks 😂 lol ok.",
    "lol nope. lol cant today 😂 lol sorry.",
    "lol sure. lol on my way 😂 lol k.",
]
STYLE_B = [
    ("Regarding the quarterly report, I reviewed the figures carefully; they look solid overall. "
     "Regarding the budget, please confirm the remaining totals by Friday afternoon."),
    ("Regarding the migration plan, the database team needs another week; the rollout moves accordingly. "
     "Regarding staffing, we should hire two engineers before the next planning cycle."),
    ("Regarding the vendor contract, legal has approved the revised terms; signatures are pending. "
     "Regarding invoices, finance will process the outstanding payments next Tuesday."),
    ("Regarding the customer escalation, support resolved the outage quickly; a postmortem follows. "
     "Regarding monitoring, the infrastructure group will add alerts for the payment service."),
    ("Regarding the roadmap review, leadership endorsed the proposal; detailed estimates are due. "
     "Regarding hiring, recruiters scheduled interviews with three promising candidates."),
    ("Regarding the security audit, the consultants found no critical issues; minor fixes remain. "
     "Regarding compliance, the documentation will be finalized before the board meeting."),
]


def write_all(store, texts, persona="Default"):
    for t in texts:
        store.write_extraction(extract(t), persona)


def a_then_b(store, clock, gap_days):
    write_all(store, STYLE_A)
    clock.advance(gap_days)
    write_all(store, STYLE_B)


def fresh(store, persona="Default"):
    store.invalidate()  # cached constraints may be slightly stale by design; decay tests want now
    return store.get_constraints(persona)


def assert_same_style(c1, c2):
    """Same constraints up to float rounding (ranked lists exactly)."""
    for k in ("top_openers", "top_transitions", "lexicon", "casing", "sample_count"):
        assert c1[k] == c2[k], k
    assert [s["signature"] for s in c1["structures"]] == [s["signature"] for s in c2["structures"]]
    assert [s["weight"] for s in c1["structures"]] == pytest.approx([s["weight"] for s in c2["structures"]],
                                                                   abs=1e-3)
    assert [b[:2] for b in c1["top_bigrams"]] == [b[:2] for b in c2["top_bigrams"]]
    assert c1["punctuation_rates"].keys() == c2["punctuation_rates"].keys()
    for sym, r in c1["punctuation_rates"].items():
        assert c2["punctuation_rates"][sym] == pytest.approx(r, abs=1e-3)
    for k in ("avg_sentence_len", "sentence_len_p90", "contractions_rate"):
        assert c1[k] == pytest.approx(c2[k], abs=0.011), k


def assert_agg_close(a, b, rel=1e-9):
    assert a.sample_count == b.sample_count
    for f in ("sample_weight", "sentence_count", "lower_starts", "upper_starts", "len_sum", "contractions",
              "expansions"):
        assert getattr(a, f) == pytest.approx(getattr(b, f), rel=rel, abs=1e-12), f
    assert a.len_hist == pytest.approx(b.len_hist, rel=rel, abs=1e-12)
    for f in ("openers", "opener_phrases", "transitions", "words", "word_totals", "bigrams", "structures",
              "punctuation"):
        da, db = getattr(a, f), getattr(b, f)
        assert da.keys() == db.keys(), f
        for key in da:
            assert da[key] == pytest.approx(db[key], rel=rel), (f, key)


def ranked(d):
    return [k for k, _ in sorted(d.items(), key=lambda kv: (-kv[1], str(kv[0])))]


# ----------------------------------------------------------------------------- helpers
def test_scale_function():
    d = D.Decay(H, clock=Clock(), t0=T0)
    assert d.scale(T0) == 1.0
    assert d.scale(T0 + H * DAY) == 2.0 and d.scale(T0 + 3 * H * DAY) == 8.0
    assert D.rebase_factor(T0, H * DAY, T0 + 2 * H * DAY) == 0.25
    off = D.Decay(0, clock=Clock(), t0=T0)
    assert not off.enabled and off.scale(T0 + 1e9) == 1.0
    assert D.rebase_factor(T0, 0, T0 + 1e9) == 1.0
    assert D.Decay(-5).half_life_s == 0  # negative = off


def test_settings_default_half_life_and_wiring(monkeypatch):
    from backend.config import Settings
    from backend.graph.store import connect_graph_store

    assert Settings().polytone_decay_half_life_days == 90.0
    s = Settings(neo4j_uri="bolt://127.0.0.1:1", polytone_decay_half_life_days=30)
    store = connect_graph_store(s, timeout=0.2)  # unreachable → in-memory fallback, same half-life
    assert store.kind == "memory" and store.fallback
    assert store.decay.half_life_days == 30
    monkeypatch.setenv("POLYTONE_DECAY_HALF_LIFE_DAYS", "0")
    assert Settings().polytone_decay_half_life_days == 0


# ----------------------------------------------------------------------------- disabled
def test_disabled_decay_ignores_time_and_keeps_integer_weights():
    """H = 0: the clock is irrelevant, weights stay ints and maintenance is a no-op."""
    clock = Clock()
    timed = InMemoryGraphStore(half_life_days=0, clock=clock)
    plain = InMemoryGraphStore()
    a_then_b(timed, clock, gap_days=10_000)
    write_all(plain, STYLE_A + STYLE_B)
    clock.advance(10_000)
    assert fresh(timed) == fresh(plain)
    assert timed._aggregate(None, 20) == plain._aggregate(None, 20)
    assert all(type(n) is int for n in timed.words.values())
    assert all(type(n) is int for n in timed.bigrams.values())
    ctx = timed.contexts["Default"]
    assert type(ctx["sentence_count"]) is int and ctx["sample_weight"] == ctx["sample_count"] == 12
    assert timed.decay_maintenance() == {}
    assert len(timed.bigrams) == len(plain.bigrams)


def test_writes_at_t0_equal_undecayed():
    """With decay on, everything written at t0 has s = 1: identical to no decay."""
    on = InMemoryGraphStore(half_life_days=H, clock=Clock())
    off = InMemoryGraphStore()
    write_all(on, STYLE_A + STYLE_B)
    write_all(off, STYLE_A + STYLE_B)
    assert on._aggregate(None, 20) == off._aggregate(None, 20)
    assert fresh(on) == fresh(off)


# ----------------------------------------------------------------------------- recency
def test_recent_style_wins():
    """A at t0, B three half-lives later: constraints follow B (A counts 1/8). Without decay the
    same corpus leans on A (it has more sentences)."""
    clock = Clock()
    s = InMemoryGraphStore(half_life_days=H, clock=clock)
    a_then_b(s, clock, gap_days=3 * H)
    mixed = InMemoryGraphStore()
    write_all(mixed, STYLE_A + STYLE_B)

    c, m = fresh(s), fresh(mixed)
    assert m["casing"] == "lower" and c["casing"] == "sentence"  # A has more sentences than B
    assert c["avg_sentence_len"] > m["avg_sentence_len"]
    b_only = InMemoryGraphStore()
    write_all(b_only, STYLE_B)
    b = fresh(b_only)
    assert abs(c["avg_sentence_len"] - b["avg_sentence_len"]) < abs(m["avg_sentence_len"] - b["avg_sentence_len"])
    assert c["top_openers"][0] == "regarding"
    assert c["punctuation_rates"][";"] > m["punctuation_rates"][";"]
    assert c["punctuation_rates"]["emoji"] < m["punctuation_rates"]["emoji"]
    assert c["lexicon"][0] not in {"lol", "ok", "u"}
    assert c["lexicon"].index("regarding") < c["lexicon"].index("lol")
    # effective recent counts: an occurrence now = 1.0, three half-lives ago = 0.125
    agg = s._aggregate(None, 20)
    assert agg.openers["regarding"] == pytest.approx(12.0)
    assert agg.openers["lol"] == pytest.approx(mixed._aggregate(None, 20).openers["lol"] / 8)


def test_idle_time_keeps_ratios_but_effective_counts_fall():
    """Decay is relative: with no new writes the constraints stay the same while every effective
    count shrinks by 2^-(idle / H)."""
    clock = Clock()
    s = InMemoryGraphStore(half_life_days=H, clock=clock)
    a_then_b(s, clock, gap_days=3 * H)
    c1, a1 = fresh(s), s._aggregate(None, 20)
    clock.advance(5 * H)
    c2, a2 = fresh(s), s._aggregate(None, 20)
    assert_same_style(c1, c2)
    assert c2["sample_count"] == c1["sample_count"] == 12
    assert a2.words["regarding"] == pytest.approx(a1.words["regarding"] / 32)
    assert a2.sentence_count == pytest.approx(a1.sentence_count / 32)
    assert ranked(a2.words) == ranked(a1.words)
    # top_bigrams carry the (falling) effective weight
    assert c2["top_bigrams"][0][2] == pytest.approx(c1["top_bigrams"][0][2] / 32, abs=1e-3)
    # thresholds scale with freshness: an all-old corpus keeps its MIN_LIST_COUNT items
    old = InMemoryGraphStore(half_life_days=H, clock=clock)
    write_all(old, STYLE_B)
    before = fresh(old)
    clock.advance(20 * H)
    assert_same_style(before, fresh(old))


def test_old_rare_items_fall_under_the_min_count():
    """An opener used twice long ago (≥ MIN_LIST_COUNT raw) is forgotten once recent samples
    dominate; the same corpus without decay keeps it."""
    texts_old = ["Anyway the thing works fine now.", "Anyway we can ship this soon."]
    recent = [f"Okay the update number {i} is ready now. Okay ship it today." for i in range(8)]
    clock = Clock()
    s = InMemoryGraphStore(half_life_days=H, clock=clock)
    write_all(s, texts_old)
    clock.advance(4 * H)
    write_all(s, recent)
    plain = InMemoryGraphStore()
    write_all(plain, texts_old + recent)
    assert "anyway" in fresh(plain)["top_openers"]
    assert "anyway" not in fresh(s)["top_openers"] and fresh(s)["top_openers"][0] == "okay"


def test_sample_count_stays_raw_and_blend_alpha_unchanged():
    clock = Clock()
    s = InMemoryGraphStore(half_life_days=H, clock=clock)
    write_all(s, STYLE_A[:1], "Friend")  # one old Friend sample
    clock.advance(4 * H)
    write_all(s, STYLE_B, "Boss")
    friend, glob = s._aggregate("Friend", 20), s._aggregate(None, 20)
    assert friend.sample_count == 1 and glob.sample_count == 7
    assert type(friend.sample_count) is int and type(glob.sample_count) is int
    assert friend.sample_weight == pytest.approx(1 / 16) and friend.freshness == pytest.approx(1 / 16)
    assert blend_weight(friend.sample_count, glob.sample_count) == 0.25  # n_p/(n_p+3), raw counts
    c = fresh(s, "Friend")
    assert c["sample_count"] == 1
    assert s.get_constraints("Nobody")["sample_count"] == 7  # pure global → global raw count
    assert s.stats()["samples"] == 7
    assert s.get_graph(40)["nodes"][0] == {"id": "c:Boss", "label": "IntentContext", "name": "Boss",
                                           "weight": 6.0}


def test_graph_view_shows_effective_weights():
    clock = Clock()
    s = InMemoryGraphStore(half_life_days=H, clock=clock)
    write_all(s, STYLE_A[:1])
    w_now = {n["name"]: n["weight"] for n in s.get_graph(80)["nodes"] if n["label"] == "Word"}
    clock.advance(H)
    g = s.get_graph(80)
    w_later = {n["name"]: n["weight"] for n in g["nodes"] if n["label"] == "Word"}
    assert w_later["lol"] == pytest.approx(w_now["lol"] / 2, abs=1e-3)
    assert all(e["weight"] > 0 for e in g["edges"])


# ----------------------------------------------------------------------------- rebase
def test_rebase_preserves_rankings_and_ratios():
    clock = Clock()
    s = InMemoryGraphStore(half_life_days=H, clock=clock, rebase_at=4.0)
    ref = InMemoryGraphStore(half_life_days=H, clock=clock)  # never rebases (REBASE_AT = 1e6)
    for store in (s, ref):
        write_all(store, STYLE_A)
    clock.advance(3 * H)  # s(now) = 8 > 4
    for store in (s, ref):
        write_all(store, STYLE_B)
    before_agg, before_c = s._aggregate(None, 20), fresh(s)
    stored_max = max(s.words.values())
    out = s.decay_maintenance()
    assert out["rebased"] == 1 and ref.decay_maintenance()["rebased"] == 0
    assert s.decay.t0 == clock() and s.decay.scale() == 1.0
    assert max(s.words.values()) == pytest.approx(stored_max / 8)  # stored values shrank…
    after_agg = s._aggregate(None, 20)
    assert_agg_close(after_agg, before_agg)  # …effective counts did not
    assert ranked(after_agg.words) == ranked(before_agg.words)
    assert ranked(after_agg.bigrams) == ranked(before_agg.bigrams)
    assert_same_style(before_c, fresh(s))
    # writes after the rebase land in the new units: still equivalent to the never-rebased store
    clock.advance(H)
    for store in (s, ref):
        write_all(store, STYLE_A[:2])
    assert_agg_close(s._aggregate(None, 20), ref._aggregate(None, 20))
    assert s.decay_maintenance()["rebased"] == 0  # s(now) = 2 < 4


def test_rebase_scales_every_decayed_value():
    clock = Clock()
    s = InMemoryGraphStore(half_life_days=H, clock=clock, rebase_at=1.5)
    write_all(s, STYLE_A[:2], "Friend")
    clock.advance(H)
    write_all(s, STYLE_B[:2], "Boss")
    snap = {
        "words": dict(s.words), "word_p": {w: dict(p) for w, p in s.word_p.items()}, "bigrams": dict(s.bigrams),
        "struct_ctx": dict(s.struct_ctx), "word_struct": dict(s.word_struct), "punct_ctx": dict(s.punct_ctx),
        "structures": dict(s.structures), "punct": dict(s.punct),
        "ctx": {n: dict(c) for n, c in s.contexts.items()},
    }
    s.decay_maintenance()
    f = 0.5
    for name in ("words", "bigrams", "struct_ctx", "word_struct", "punct_ctx", "structures", "punct"):
        cur = getattr(s, name)
        assert {k: v for k, v in cur.items()} == pytest.approx({k: v * f for k, v in snap[name].items()
                                                               if k in cur}), name
    assert s.word_p["lol"]["Friend"] == pytest.approx(snap["word_p"]["lol"]["Friend"] * f)
    for n, c in s.contexts.items():
        old = snap["ctx"][n]
        assert c["sample_count"] == old["sample_count"]  # raw: not scaled
        for k in D.CTX_DECAYED_SCALARS:
            assert c[k] == pytest.approx(old[k] * f), k
        for k in D.CTX_DECAYED_LISTS:
            assert c[k] == pytest.approx([x * f for x in old[k]]), k


# ----------------------------------------------------------------------------- prune
def test_prune_removes_faded_edges_and_words_keeps_recent():
    clock = Clock()
    s = InMemoryGraphStore(half_life_days=1, clock=clock)
    s.write_extraction(extract("zebra quokka marmot wombat. the zebra sleeps."), "Default")
    clock.advance(10)  # 2^-10 ≈ 0.001 < PRUNE_BELOW
    recent = "the cat sat on the mat. the dog sat too."
    s.write_extraction(extract(recent), "Default")
    size = (len(s.words), len(s.bigrams), len(s.word_struct))
    out = s.decay_maintenance()
    assert out["rebased"] == 0 and out["followed_by"] > 0 and out["part_of"] > 0 and out["words"] > 0
    assert (len(s.words), len(s.bigrams), len(s.word_struct)) < size
    assert size[1] - len(s.bigrams) == out["followed_by"] and size[0] - len(s.words) == out["words"]
    for w in ("zebra", "quokka", "marmot", "wombat", "sleeps"):
        assert w not in s.words and w not in s.word_p
    assert ("zebra", "quokka") not in s.bigrams and ("the", "zebra") not in s.bigrams
    assert ("the", "cat") in s.bigrams and ("sat", "on") in s.bigrams
    assert "the" in s.words  # shared word: recent edges keep it
    # adjacency and the bigram table agree after pruning
    assert {(a, b) for a, bs in s.succ.items() for b in bs} == set(s.bigrams)
    assert {(a, b) for b, as_ in s.pred.items() for a in as_} == set(s.bigrams)
    assert all(w in s.words for w, _ in s.word_struct)
    c = fresh(s)
    assert "zebra" not in c["lexicon"] and "cat" in c["lexicon"]
    assert s.decay_maintenance() == {"rebased": 0, "followed_by": 0, "part_of": 0, "words": 0}


def test_prune_keeps_edges_above_threshold():
    clock = Clock()
    s = InMemoryGraphStore(half_life_days=1, clock=clock)
    s.write_extraction(extract("alpha beta gamma."), "Default")
    clock.advance(4)  # 2^-4 = 0.0625 ≥ 0.05: still remembered
    assert s.decay_maintenance()["followed_by"] == 0
    clock.advance(0.5)  # 2^-4.5 ≈ 0.044 < 0.05
    out = s.decay_maintenance()
    assert out["followed_by"] == 2 and out["words"] == 3 and not s.bigrams and not s.words


# ----------------------------------------------------------------------------- persona merge
def test_merge_persona_with_float_weights():
    clock = Clock()
    merged = InMemoryGraphStore(half_life_days=H, clock=clock)
    direct = InMemoryGraphStore(half_life_days=H, clock=clock)
    write_all(merged, STYLE_A[:3], "Friend")
    write_all(direct, STYLE_A[:3], "Boss")
    clock.advance(1.5 * H)
    write_all(merged, STYLE_B[:3], "Boss")
    write_all(direct, STYLE_B[:3], "Boss")
    assert merged.merge_persona("Friend", "Boss") == 3
    assert "Friend" not in merged.contexts
    ctx = merged.contexts["Boss"]
    assert ctx["sample_count"] == 6 and type(ctx["sample_count"]) is int
    assert isinstance(ctx["sample_weight"], float) and ctx["sample_weight"] == pytest.approx(3 + 3 * 2 ** 1.5)
    assert_agg_close(merged._aggregate("Boss", 20), direct._aggregate("Boss", 20))
    assert_agg_close(merged._aggregate(None, 20), direct._aggregate(None, 20))
    assert_same_style(fresh(merged, "Boss"), fresh(direct, "Boss"))
    # fractional per-persona weights are summed, not truncated
    the_old = sum(extract(t).words.get("the", 0) for t in STYLE_A[:3])
    the_new = sum(extract(t).words.get("the", 0) for t in STYLE_B[:3])
    assert the_old and the_new
    want = the_old + the_new * 2 ** 1.5  # stored units: Friend at s = 1, Boss at s = 2^1.5
    assert merged.word_p["the"]["Boss"] == pytest.approx(want) and not want.is_integer()
    assert merged.word_p["the"]["Boss"] == pytest.approx(direct.word_p["the"]["Boss"])


def test_merged_context_keeps_floats_and_legacy_sample_weight():
    from backend.graph.store import merged_context

    legacy = {"sample_count": 4, "sentence_count": 9, "len_hist": [1, 2], "opener_keys": ["a"],
              "opener_counts": [3]}  # written before decay: no sample_weight
    new = {"sample_count": 1, "sample_weight": 2.5, "sentence_count": 2.5, "len_hist": [0.5, 0, 2.5],
           "opener_keys": ["a", "b"], "opener_counts": [1.25, 2.5]}
    out = merged_context(legacy, new, texts=False)
    assert out["sample_count"] == 5 and out["sample_weight"] == 6.5
    assert out["sentence_count"] == 11.5 and out["len_hist"] == [1.5, 2, 2.5]
    assert out["opener_keys"] == ["a", "b"] and out["opener_counts"] == [4.25, 2.5]


# ----------------------------------------------------------------------------- lock
def test_rebase_lock_excludes_shared_holders():
    import threading
    import time

    lock = D.RebaseLock()
    events = []
    with lock.shared():
        t = threading.Thread(target=lambda: (lock.exclusive().__enter__(), events.append("x")))
        t.start()
        time.sleep(0.05)
        assert events == []  # exclusive waits for the shared holder
    t.join(1)
    assert events == ["x"]


# ----------------------------------------------------------------------------- scheduling
def _decay_runtime(settings, tmp_path, clock):
    from backend.llm.client import MockLLM
    from backend.runtime import build_runtime

    store = InMemoryGraphStore(half_life_days=1, clock=clock)
    return build_runtime(settings, db_path=tmp_path / "decay.sqlite3", store=store, llm=MockLLM(), scorer=False)


def _maintenance_events(rt):
    return [e for e in rt.audit.entries(limit=1000) if e["event"] == "graph.decay_maintenance"]


def test_retention_task_runs_decay_maintenance_and_audits_counts(settings, tmp_path):
    from backend.security import retention

    clock = Clock()
    rt = _decay_runtime(settings, tmp_path, clock)
    try:
        rt.store.write_extraction(extract("zebra quokka marmot. zebra again."), "Default")
        clock.advance(15)  # 2^-15 ≪ PRUNE_BELOW, s = 2^15 < REBASE_AT
        rt.store.write_extraction(extract("the cat sat."), "Default")
        t = retention.RetentionTask(rt, interval_s=3600).start()
        t.stop()
        (ev,) = _maintenance_events(rt)
        d = ev["detail"]
        assert d["half_life_days"] == 1 and d["rebased"] == 0
        assert d["followed_by"] >= 3 and d["words"] >= 3
        assert set(d) == {"half_life_days", "rebased", "followed_by", "part_of", "words"}
        assert not any(isinstance(v, str) for v in d.values())  # counts only, never user text
        assert "zebra" not in rt.store.words
        assert retention.graph_maintenance(rt) == {"rebased": 0, "followed_by": 0, "part_of": 0, "words": 0}
        assert len(_maintenance_events(rt)) == 1  # nothing changed → not audited
    finally:
        rt.close()


def test_server_startup_runs_decay_maintenance(settings, tmp_path):
    import time

    from backend.tests.conftest import api_client

    clock = Clock()
    rt = _decay_runtime(settings, tmp_path, clock)
    try:
        rt.store.write_extraction(extract("zebra quokka marmot."), "Default")
        clock.advance(15)  # 2^-15 ≪ PRUNE_BELOW, s = 2^15 < REBASE_AT
        with api_client(rt) as c:
            deadline = time.monotonic() + 5
            while not _maintenance_events(rt) and time.monotonic() < deadline:
                time.sleep(0.02)
            assert _maintenance_events(rt), "start-up maintenance did not run"
            assert c.get("/health").status_code == 200
        assert not rt.store.bigrams
    finally:
        rt.close()


def test_failing_maintenance_never_crashes_the_task(settings, tmp_path, caplog):
    from backend.security import retention

    clock = Clock()
    rt = _decay_runtime(settings, tmp_path, clock)

    def boom():
        raise RuntimeError("secret user text must not be logged")

    rt.store.decay_maintenance = boom  # type: ignore[method-assign]
    try:
        assert retention.graph_maintenance(rt) == {}
        t = retention.RetentionTask(rt, interval_s=3600).start()
        t.stop()
        assert "graph decay maintenance failed: RuntimeError" in caplog.text
        assert "secret user text" not in caplog.text
        assert _maintenance_events(rt) == []
    finally:
        rt.close()


def test_maintenance_invalidates_cached_constraints():
    clock = Clock()
    s = InMemoryGraphStore(half_life_days=1, clock=clock)
    s.write_extraction(extract("zebra quokka marmot wombat. zebra quokka again."), "Default")
    clock.advance(10)
    s.write_extraction(extract("the cat sat on the mat."), "Default")
    before = s.get_constraints("Default")
    assert any(b[:2] == ["zebra", "quokka"] for b in before["top_bigrams"])
    s.decay_maintenance()
    after = s.get_constraints("Default")  # no manual invalidate: maintenance did it
    assert not any(b[:2] == ["zebra", "quokka"] for b in after["top_bigrams"])
    assert math.isclose(after["sample_count"], before["sample_count"])
