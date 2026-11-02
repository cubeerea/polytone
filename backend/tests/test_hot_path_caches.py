"""Hot-path caches: incremental draft index, few-shot memo, constraint cache, warm-up."""
from __future__ import annotations

import random
import threading

from backend.graph.store import InMemoryGraphStore
from backend.ingestion import firewall
from backend.ingestion.firewall import draft_match_reason, draft_snapshot
from backend.ingestion.text import containment, jaccard, shingles, tokenize
from backend.langgraph import examples
from backend.langgraph.examples import select_examples
from backend.langgraph.ingest_graph import run_ingest, run_ingest_batch
from backend.storage.db import Database, sha256_text

WORDS = ["the", "build", "broke", "again", "so", "we", "push", "the", "launch", "to", "friday", "and", "i", "will", "send", "the", "deck", "later", "today"]


def _full_scan_reason(text: str, drafts: list[str]) -> str | None:
    """The pre-index algorithm (compare against every draft), kept as the reference."""
    if not drafts:
        return None
    if sha256_text(text) in {sha256_text(d) for d in drafts}:
        return "matches a Polytone-generated draft"
    norm = " " + " ".join(tokenize(text)) + " "
    n_toks = norm.count(" ") - 1
    sh = shingles(text)
    for d in drafts:
        d_norm = " " + " ".join(tokenize(d)) + " "
        d_len, d_sh = d_norm.count(" ") - 1, shingles(d)
        if d_len >= 4 and d_norm in norm:
            return "contains a Polytone-generated draft"
        if n_toks >= 4 and norm in d_norm:
            return "is an excerpt of a Polytone-generated draft"
        if jaccard(sh, d_sh) >= 0.8:
            return "near-duplicate of a Polytone-generated draft"
        if d_len >= 4 and containment(d_sh, sh) >= 0.8:
            return "contains a near-copy of a Polytone-generated draft"
    return None


def test_draft_index_matches_full_scan(tmp_path):
    rng = random.Random(7)
    db = Database(tmp_path / "d.sqlite3")
    drafts: list[str] = []
    for i in range(120):
        text = " ".join(rng.choice(WORDS) for _ in range(rng.randint(1, 14)))
        if text.strip() and sha256_text(text) not in {sha256_text(d) for d in drafts}:
            db.insert_draft(persona="P", intent="i", text=text)
            drafts.append(text)
        probes = [" ".join(rng.choice(WORDS) for _ in range(rng.randint(1, 20))) for _ in range(5)]
        probes += [drafts[-1], "prefix words " + drafts[rng.randrange(len(drafts))] + " suffix"]
        for p in probes:
            assert draft_match_reason(p, db) == _full_scan_reason(p, drafts), p


def test_draft_index_sees_other_process_writes_and_deletes(tmp_path):
    a = Database(tmp_path / "x.sqlite3")
    b = Database(tmp_path / "x.sqlite3")  # e.g. an MCP server process writing the same file
    text = "sounds good, i will send the deck over tonight"
    assert draft_match_reason(text, a) is None
    b.insert_draft(persona="P", intent="i", text=text)
    assert draft_match_reason(text, a) == "matches a Polytone-generated draft"
    snap = draft_snapshot(a)
    with b.connect() as c:
        c.execute("DELETE FROM drafts")
    assert draft_match_reason(text, a) is None  # deletion → full rebuild
    assert snap.has_hash(sha256_text(text))  # an older snapshot is unaffected (append-only view)


def test_draft_index_sees_new_draft_that_reuses_a_deleted_rowid(tmp_path):
    # wipe (or a retention purge) deletes every draft; SQLite then hands the next draft the same
    # rowid, so COUNT and MAX(rowid) are what the index saw before: it must still notice the swap
    db = Database(tmp_path / "r.sqlite3")
    old, new = "old draft about the launch on friday", "sounds good, i will send the deck over tonight"
    db.insert_draft(persona="P", intent="i", text=old)
    assert draft_match_reason(old, db) == "matches a Polytone-generated draft"
    db.wipe_user_data()
    db.insert_draft(persona="P", intent="i", text=new)
    assert draft_match_reason(new, db) == "matches a Polytone-generated draft"
    assert draft_match_reason(old, db) is None
    # more drafts than before the wipe: MAX(rowid) grows, but rowid 1 is a different draft again
    db.wipe_user_data()
    db.insert_draft(persona="P", intent="i", text=old)
    db.insert_draft(persona="P", intent="i", text="another draft that goes to the team")
    assert draft_match_reason(old, db) == "matches a Polytone-generated draft"
    assert draft_match_reason(new, db) is None


def test_draft_index_threads(tmp_path):
    db = Database(tmp_path / "t.sqlite3")
    errors: list[BaseException] = []

    def writer(i):
        try:
            db.insert_draft(persona="P", intent="i", text=f"draft number {i} goes out on friday")
            assert draft_match_reason(f"draft number {i} goes out on friday", db) is not None
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    ts = [threading.Thread(target=writer, args=(i,)) for i in range(16)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors and draft_snapshot(db).n == 16


def _corpus(rt, persona="Boss", n=12):
    rng = random.Random(3)
    for i in range(n):
        body = ". ".join(" ".join(rng.choice(WORDS) for _ in range(rng.randint(4, 12))) for _ in range(3))
        assert run_ingest(f"{body}. ref {i}.", "manual", persona, {}, runtime=rt)["accepted"] == 1


def test_examples_memo_is_exact_and_rechecks_new_drafts(runtime):
    _corpus(runtime)
    c = runtime.store.get_constraints("Boss")
    first = select_examples(runtime.db, "Boss", "push the launch to friday", c)
    assert len(first) >= 2
    assert select_examples(runtime.db, "Boss", "push the launch to friday", c) == first  # memo hit
    # a new draft equal to a chosen excerpt must evict it, exactly like a fresh computation
    runtime.db.insert_draft(persona="Boss", intent="x", text=first[0])
    after = select_examples(runtime.db, "Boss", "push the launch to friday", c)
    assert first[0] not in after
    examples._picks.clear()
    assert select_examples(runtime.db, "Boss", "push the launch to friday", c) == after
    # a new sample changes the samples watermark → recomputed
    run_ingest("totally new words about quarterly numbers and hiring plans here.", "manual", "Boss", {},
               runtime=runtime)
    key_count = len(examples._picks)
    select_examples(runtime.db, "Boss", "push the launch to friday", c)
    assert len(examples._picks) == key_count + 1


def test_constraint_cache_skips_results_that_raced_a_write():
    store = InMemoryGraphStore()
    from backend.ingestion.extract import extract

    store.write_extraction(extract("hey — we ship friday. thanks so much!"), "Friend")
    real = store._aggregate

    def racing(persona, k):  # a write lands while constraints are being computed
        agg = real(persona, k)
        if persona is None and not racing.done:
            racing.done = True
            store.write_extraction(extract("Formal update: the migration is complete."), "Boss")
        return agg

    racing.done = False
    store._aggregate = racing
    store.get_constraints("Friend")
    assert ("Friend", 20) not in store._cache and not store._global  # raced result not cached
    store._aggregate = real
    store.get_constraints("Friend")
    assert ("Friend", 20) in store._cache and store._global[20].sample_count == 2


def test_global_aggregate_shared_between_personas(monkeypatch):
    store = InMemoryGraphStore()
    from backend.ingestion.extract import extract

    store.write_extraction(extract("so the build broke again. fixing it now."), "Friend")
    store.write_extraction(extract("Quick update: the migration is done."), "Boss")
    calls = []
    real = store._aggregate
    monkeypatch.setattr(store, "_aggregate", lambda p, k: calls.append(p) or real(p, k))
    store.get_constraints("Friend")
    store.get_constraints("Boss")
    assert calls == [None, "Friend", "Boss"]  # the global aggregate is read once


def test_ingest_primes_constraints_in_background(runtime):
    runtime.score_async = True
    runtime.db.set_config(active_persona="Boss")
    run_ingest_batch(["so the build broke again, fixing it now."], "manual", "Boss", runtime=runtime)
    runtime._executor.shutdown(wait=True)
    runtime._executor = None
    assert ("Boss", 20) in runtime.store._cache


def test_firewall_still_blocks_drafts_end_to_end(runtime):
    from backend.langgraph.generate_graph import run_generation

    _corpus(runtime, "Friend", 4)
    out = run_generation("- tell sam the meeting moved to 3\n- ask for the slides", "Friend", None, runtime=runtime)
    res = run_ingest(out["text"], "manual", "Friend", {}, runtime=runtime)
    assert res["accepted"] == 0 and "draft" in res["reasons"][0]
    assert firewall.check("prefix " + out["text"] + " and more words", "manual", {}, runtime.db)[0] is False
