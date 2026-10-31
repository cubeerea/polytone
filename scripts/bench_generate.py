"""Benchmark the POST /generate hot path, node by node.

    uv run python scripts/bench_generate.py                       # in-memory graph store
    uv run python scripts/bench_generate.py --neo4j bolt://localhost:7688 --neo4j-password bench-pass
    uv run python scripts/bench_generate.py --samples 400 --drafts 1000 --runs 50 --http

Always uses the mock LLM, no embeddings, and a throwaway SQLite in a temp dir (never the
user's data/). With ``--neo4j`` the target database is WIPED first — point it only at a
scratch container, never at the real style graph on :7687 (refused).

Reports p50/p95 (ms) per LangGraph node, the node sum, the end-to-end ``run_generation`` time
(includes LangGraph overhead) and, with ``--http``, the FastAPI ``POST /generate`` round trip.
"""
from __future__ import annotations

import argparse
import os
import random
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

os.environ["POLYTONE_LLM"] = "mock"
os.environ["POLYTONE_EMBEDDINGS"] = "none"
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ.setdefault("POLYTONE_WATCHER", "0")
os.environ.setdefault("POLYTONE_WISPR_LIVE", "0")
os.environ.setdefault("WISPR_HISTORY_DIRS", "/nonexistent")
# throwaway data key: a benchmark never reads or creates the real Keychain item
os.environ["POLYTONE_DATA_KEY"] = __import__("base64").b64encode(os.urandom(32)).decode()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import Settings
from backend.langgraph import generate_graph

NODE_ORDER = ["capture", "retrieve_constraints", "build_prompt", "generate", "postprocess", "record", "score"]
TIMINGS: dict[str, list[float]] = defaultdict(list)

_OrigStateGraph = generate_graph.StateGraph


class _TimedStateGraph(_OrigStateGraph):  # type: ignore[misc,valid-type]
    """Wraps every node so its wall time lands in TIMINGS (works on any version of the graph)."""

    def add_node(self, name, fn=None, **kw):  # type: ignore[override]
        def timed(state, _fn=fn, _name=name):
            t0 = time.perf_counter()
            try:
                return _fn(state)
            finally:
                TIMINGS[_name].append((time.perf_counter() - t0) * 1000)

        return super().add_node(name, timed, **kw)


generate_graph.StateGraph = _TimedStateGraph

# ----------------------------------------------------------------------------- synthetic data
_OPENERS = ["hey", "so", "honestly", "quick update", "just wanted to say", "ok so", "fwiw", "btw", "thanks", "i think"]
_SUBJ = ["the build", "the deck", "our launch", "the demo", "the q3 plan", "the migration", "the release", "the doc"]
_VERB = ["is blocked on", "looks good for", "slipped past", "needs another pass before", "is ready for", "depends on"]
_OBJ = ["friday", "the review", "legal sign-off", "the board meeting", "next sprint", "the customer call", "lunch"]
_TAIL = ["— no rush", "!", "... will check later", ", let me know", ". thanks!", " :)", ". more soon", "?"]
_PERSONAS = ["Default", "Boss", "Friend", "Investor"]


def _sentence(rng: random.Random) -> str:
    s = f"{rng.choice(_OPENERS)} {rng.choice(_SUBJ)} {rng.choice(_VERB)} {rng.choice(_OBJ)}{rng.choice(_TAIL)}"
    return s if rng.random() < 0.5 else s[0].upper() + s[1:]


def _sample(rng: random.Random, i: int) -> str:
    return " ".join(_sentence(rng) for _ in range(rng.randint(6, 14))) + f" (ref {i})"


INTENTS = [
    "- tell sam the meeting moved to 3\n- ask for the deck",
    "say thanks for the quick review",
    "ask if we can push the launch to next week",
    "- the build is fixed\n- deploy tonight\n- ask jo to check the dashboard",
    "let alex know the board meeting is confirmed for friday",
]


def seed(rt, n_samples: int, n_drafts: int, rng: random.Random) -> None:
    from backend.langgraph.ingest_graph import run_ingest_batch

    by_persona: dict[str, list] = defaultdict(list)
    for i in range(n_samples):
        by_persona[_PERSONAS[i % len(_PERSONAS)]].append(
            {"text": _sample(rng, i), "provenance": {"source": "manual", "generated": False}}
        )
    t0 = time.perf_counter()
    for persona, items in by_persona.items():
        r = run_ingest_batch(items, "manual", persona, runtime=rt)
        assert r["accepted"] == len(items), r
    t1 = time.perf_counter()
    for i in range(n_drafts):
        rt.db.insert_draft(
            persona=_PERSONAS[i % 4], intent="x", text=f"Draft {i}: " + _sentence(rng) + " " + _sentence(rng)
        )
    print(f"seeded {n_samples} samples in {t1 - t0:.1f}s, {n_drafts} drafts in {time.perf_counter() - t1:.1f}s")


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--samples", type=int, default=400)
    ap.add_argument("--drafts", type=int, default=1000)
    ap.add_argument("--runs", type=int, default=50)
    ap.add_argument("--warmup", type=int, default=3)
    ap.add_argument("--neo4j", default=None, help="bolt URI of a SCRATCH Neo4j (wiped!)")
    ap.add_argument("--neo4j-user", default="neo4j")
    ap.add_argument("--neo4j-password", default="bench-pass")
    ap.add_argument("--http", action="store_true", help="also time POST /generate through FastAPI")
    ap.add_argument(
        "--ingest-every", type=int, default=0,
        help="ingest one new sample every N runs (exercises constraint-cache invalidation)",
    )
    args = ap.parse_args()

    from backend.graph.store import InMemoryGraphStore, Neo4jGraphStore
    from backend.llm.client import MockLLM
    from backend.runtime import build_runtime

    tmp = Path(tempfile.mkdtemp(prefix="polytone-bench-"))
    settings = Settings(
        polytone_data_dir=tmp, polytone_llm="mock", polytone_embeddings="none",
        anthropic_api_key="", polytone_watch_dir=tmp / "inbox",
    )
    if args.neo4j:
        if args.neo4j.rstrip("/").endswith(":7687"):
            sys.exit("refusing to wipe :7687 — that is the user's real style graph")
        store = Neo4jGraphStore(args.neo4j, args.neo4j_user, args.neo4j_password, timeout=5)
        store.ensure_schema()
        store._driver.execute_query("MATCH (n) DETACH DELETE n")
    else:
        store = InMemoryGraphStore()
    rt = build_runtime(settings, db_path=tmp / "bench.sqlite3", store=store, llm=MockLLM(), scorer=False, score_async=True)  # like the server
    rng = random.Random(42)
    seed(rt, args.samples, args.drafts, rng)

    from backend.langgraph.generate_graph import run_generation
    from backend.langgraph.ingest_graph import run_ingest

    totals: list[float] = []
    for i in range(args.warmup + args.runs):
        if args.ingest_every and i and i % args.ingest_every == 0:
            run_ingest(_sample(rng, 10_000 + i), "manual", _PERSONAS[i % 4], runtime=rt)
        if i == args.warmup:
            TIMINGS.clear()
        t0 = time.perf_counter()
        run_generation(INTENTS[i % len(INTENTS)], _PERSONAS[i % 4], "Slack", runtime=rt)
        if i >= args.warmup:
            totals.append((time.perf_counter() - t0) * 1000)

    http: list[float] = []
    if args.http:
        from fastapi.testclient import TestClient

        from backend.daemon.server import create_app

        app = create_app(rt)
        with TestClient(
            app,
            base_url=f"http://127.0.0.1:{settings.polytone_api_port}",  # the Host check wants loopback
            headers={"Authorization": f"Bearer {app.state.auth.app_token}"},
        ) as c:
            for i in range(args.warmup + args.runs):
                t0 = time.perf_counter()
                r = c.post("/generate", json={"intent": INTENTS[i % 5], "persona": _PERSONAS[i % 4]})
                assert r.status_code == 200, r.text
                if i >= args.warmup:
                    http.append((time.perf_counter() - t0) * 1000)

    store_kind = f"neo4j ({args.neo4j})" if args.neo4j else "memory"
    print(f"\nstore={store_kind} samples={args.samples} drafts={args.drafts}+ runs={args.runs}")
    print(f"{'node':<22}{'p50 ms':>10}{'p95 ms':>10}{'mean ms':>10}")
    node_sum: list[float] = [0.0] * args.runs
    for name in NODE_ORDER:
        xs = TIMINGS.get(name, [])[-args.runs:]
        for j, x in enumerate(xs):
            node_sum[j] += x
        print(f"{name:<22}{pct(xs, .5):>10.2f}{pct(xs, .95):>10.2f}{statistics.fmean(xs) if xs else 0:>10.2f}")
    print(f"{'(sum of nodes)':<22}{pct(node_sum, .5):>10.2f}{pct(node_sum, .95):>10.2f}{statistics.fmean(node_sum):>10.2f}")
    print(f"{'run_generation total':<22}{pct(totals, .5):>10.2f}{pct(totals, .95):>10.2f}{statistics.fmean(totals):>10.2f}")
    if http:
        print(f"{'HTTP POST /generate':<22}{pct(http, .5):>10.2f}{pct(http, .95):>10.2f}{statistics.fmean(http):>10.2f}")
    rt.close()


if __name__ == "__main__":
    main()
