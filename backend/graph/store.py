"""Style graph storage: ``GraphStore`` protocol, ``Neo4jGraphStore``, ``InMemoryGraphStore``.

Only human text (post-firewall) is ever written here — generated drafts never are.

Persona semantics: every write is attributed to ``IntentContext {name: persona}``.
``get_constraints(persona)`` reads two aggregates via parameterized Cypher — the persona's and
the all-persona one — and blends them (``graph/constraints.py`` documents the blend: a persona
with few samples leans on the user's overall style; with none it is pure global, so a fresh
"Boss" persona still sounds like the user). ``sample_count`` is the persona's own sample count
(the global count when it has none). Constraints are cached per (persona, k) and the cache is
invalidated on every write (a generation counter keeps a computation that raced with a write
out of the cache). The all-persona aggregate is cached separately, so after an ingest each
persona's first request only re-reads its own aggregate.

Encryption at rest (see ``graph/queries.py``): Neo4j holds blind indexes (lookup keys) and
ciphertexts, never the user's words; ``Neo4jGraphStore`` decrypts on read through an in-memory
``key -> word`` map (filled by writes and by the first read of each key), so a warm read decrypts
nothing. ``InMemoryGraphStore`` keeps plaintext (RAM only, nothing at rest) and breaks ranking
ties on the same blind index, so both stores return identical aggregates.

Scalability (see ``graph/trim.py``): every write is trimmed to bounded counts first, and after it
each touched word keeps at most ``GRAPH_FANOUT`` FOLLOWED_BY edges per direction, so common words
("the", "and") never grow into unbounded supernodes. Both stores apply the same rules.

Recency (see ``graph/decay.py``): writes are multiplied by the store's ``s(now)`` (computed once in
``trim.prepare``) and every read divides by ``s(now)``, so aggregates are effective recent counts
while nothing stored is rewritten. ``decay_maintenance()`` (daily) rebases when ``s`` gets large
and prunes edges that faded away. Each store has an injectable clock and half-life
(``POLYTONE_DECAY_HALF_LIFE_DAYS``, wired by ``connect_graph_store``; 0 = off, the default for a
store built directly). Neo4j keeps ``t0`` in ``(:PolytoneMeta {name:'schema'})``; the in-memory
store as an attribute. Within a process the constraints cache may serve results computed a little
earlier (decay is slow); maintenance invalidates it.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from backend.graph import queries as Q
from backend.graph.constraints import (
    Constraints,
    StyleAggregate,
    compute_constraints,
    empty_constraints,
    sample_weight,
)
from backend.graph.decay import (
    PRUNE_BELOW,
    REBASE_AT,
    Decay,
    RebaseLock,
    rebase_factor,
    scale_context,
)
from backend.graph.trim import GRAPH_FANOUT, chunks, prepare
from backend.ingestion.types import Extraction
from backend.security import crypto

log = logging.getLogger(__name__)

_WORD_FETCH = 400  # words pulled for lexicon computation
_BIGRAM_SEED = 150  # bigrams are expanded from this many top words
_STRUCT_FETCH = 100  # structures pulled per aggregate before blending

# blind-index / AAD contexts of the encrypted graph properties
WORD_CTX = "neo4j:Word.text"
CTX_LISTS = (  # (aggregate field, keys property, texts property)
    ("openers", "opener_keys", "opener_texts"),
    ("opener_phrases", "opener_phrase_keys", "opener_phrase_texts"),
    ("transitions", "transition_keys", "transition_texts"),
)
MIGRATE_BATCH = 2000
DECAY_BATCH = 2000  # rows per transaction for rebase / prune


def word_key(w: str) -> str:
    return crypto.blind_index(w, WORD_CTX)


def list_key(prop: str, value: str) -> str:
    return crypto.blind_index(value, f"neo4j:IntentContext.{prop}")


@runtime_checkable
class GraphStore(Protocol):
    kind: str  # "neo4j" | "memory"

    def ensure_schema(self) -> None: ...

    def write_extraction(self, extraction: Extraction | dict[str, Any], persona: str) -> dict[str, int]:
        """Merge one human sample's extraction. Returns {"words_merged", "sentences"}."""
        ...

    def get_constraints(self, persona: str | None, k: int = 20) -> Constraints: ...

    def get_graph(self, limit: int = 60, persona: str | None = None) -> dict[str, list[dict]]: ...

    def stats(self) -> dict[str, Any]: ...

    def healthy(self) -> bool: ...

    def wipe(self) -> int:
        """Delete the whole style graph. Returns the nodes deleted."""
        ...

    def merge_persona(self, src: str, dst: str) -> int:
        """Fold everything learned under persona ``src`` into ``dst`` and remove ``src`` (a deleted
        persona's samples move to Default). All-persona totals are unchanged. Returns the
        number of samples ``src`` had (0 when it had no IntentContext)."""
        ...

    def migrate_encryption(self) -> dict[str, int]:
        """Encrypt a legacy plaintext graph in place (idempotent). Returns counts."""
        ...

    def decay_maintenance(self) -> dict[str, int]:
        """Rebase the decay epoch if ``s(now)`` got large, then prune edges / words whose
        effective weight faded below ``PRUNE_BELOW``. Returns counts (``{}`` when decay is off)."""
        ...

    def close(self) -> None: ...


# ============================================================================ shared base
class _BaseStore:
    kind = "base"

    def __init__(self, decay: Decay | None = None) -> None:
        self.decay = decay or Decay()
        self._cache: dict[tuple[str | None, int], Constraints] = {}
        self._global: dict[int, StyleAggregate] = {}  # all-persona aggregate, shared by every persona
        self._gen = 0  # bumped by every write; results computed under an older gen are not cached
        self._cache_lock = threading.Lock()

    # -- to implement ---------------------------------------------------------------
    def _aggregate(self, persona: str | None, k: int) -> StyleAggregate:  # pragma: no cover
        raise NotImplementedError

    def _top_nodes(self, persona: str | None, n_words: int, n_structs: int) -> dict[str, list]:
        raise NotImplementedError  # pragma: no cover

    def _edges_among(
        self, persona: str | None, words: list[str], sigs: list[str], syms: list[str], ctxs: list[str]
    ) -> dict[str, list]:
        raise NotImplementedError  # pragma: no cover

    def _read_scale(self) -> float:
        """``s(now)`` to divide stored weights by (Neo4j loads ``t0`` first)."""
        return self.decay.scale()

    # -- cache ---------------------------------------------------------------------
    def invalidate(self) -> None:
        with self._cache_lock:
            self._gen += 1
            self._cache.clear()
            self._global.clear()

    def get_constraints(self, persona: str | None, k: int = 20) -> Constraints:
        key = (persona or None, k)
        with self._cache_lock:
            hit = self._cache.get(key)
            g = self._global.get(k)
            gen = self._gen
        if hit is not None:
            return _copy(hit)
        if g is None:
            g = self._aggregate(None, k)
        p = self._aggregate(persona, k) if persona and g.sample_count else g
        c = compute_constraints(p, k, global_agg=g) if g.sample_count else empty_constraints()
        with self._cache_lock:
            if gen == self._gen:  # no write raced with this computation
                self._cache[key] = c
                self._global[k] = g
        return _copy(c)

    # -- graph view ----------------------------------------------------------------
    def get_graph(self, limit: int = 60, persona: str | None = None) -> dict[str, list[dict]]:
        limit = max(1, int(limit))
        persona = persona or None
        s = self._read_scale()

        def eff(w: Any) -> float:  # stored weight → effective recent count (display: 3 decimals)
            return round(float(w or 0) / s, 3) if s != 1.0 else float(w or 0)

        top = self._top_nodes(persona, n_words=limit, n_structs=max(1, limit // 5))
        nodes: list[dict] = []
        ctxs = top["contexts"][: max(1, limit // 10)]
        syms = top["punctuation"][:8]
        budget = limit - len(ctxs) - len(syms)
        structs = top["structures"][: max(0, min(len(top["structures"]), budget // 4))]
        words = top["words"][: max(0, budget - len(structs))]
        for name, n in ctxs:  # raw sample count (not decayed)
            nodes.append({"id": f"c:{name}", "label": "IntentContext", "name": name, "weight": float(n)})
        for sym, w in syms:
            nodes.append({"id": f"p:{sym}", "label": "PunctuationMark", "name": sym, "weight": eff(w)})
        for sig, w in structs:
            nodes.append({"id": f"s:{sig}", "label": "SentenceStructure", "name": sig, "weight": eff(w)})
        for text, w in words:
            nodes.append({"id": f"w:{text}", "label": "Word", "name": text, "weight": eff(w)})
        nodes = nodes[:limit]
        ids = {n["id"] for n in nodes}
        e = self._edges_among(
            persona,
            [t for t, _ in words],
            [s for s, _ in structs],
            [p for p, _ in syms],
            [c for c, _ in ctxs],
        )
        edges: list[dict] = []
        for a, b, w in e["followed"]:
            edges.append({"source": f"w:{a}", "target": f"w:{b}", "type": "FOLLOWED_BY", "weight": eff(w)})
        for w_, sig, w in e["part_of"]:
            edges.append(
                {"source": f"w:{w_}", "target": f"s:{sig}", "type": "PART_OF_STRUCTURE", "weight": eff(w)}
            )
        for kind, key, ctx, w in e["used_in"]:
            edges.append({"source": f"{kind}:{key}", "target": f"c:{ctx}", "type": "USED_IN", "weight": eff(w)})
        edges = [x for x in edges if x["source"] in ids and x["target"] in ids and x["weight"] > 0]
        edges.sort(key=lambda x: -x["weight"])
        return {"nodes": nodes, "edges": edges}

    def close(self) -> None:
        pass

    def migrate_encryption(self) -> dict[str, int]:
        return {}


_CTX_SCALARS = ("sentence_count", "lower_starts", "upper_starts", "len_sum", "contractions", "expansions")
_CTX_KEYED = (  # (keys, counts, texts | None) — texts are the Neo4j ciphertexts parallel to keys
    ("opener_keys", "opener_counts", "opener_texts"),
    ("opener_phrase_keys", "opener_phrase_counts", "opener_phrase_texts"),
    ("transition_keys", "transition_counts", "transition_texts"),
)


def merged_context(dst: dict[str, Any] | None, src: dict[str, Any], *, texts: bool) -> dict[str, Any]:
    """IntentContext properties of ``dst`` with ``src`` added in (same shape as a write). Keyed
    lists merge by key; with ``texts`` the parallel ciphertext lists follow their keys.
    ``sample_count`` stays an integer; every other counter is a (possibly float) decayed weight
    and is summed as is — both contexts are in the same units (same ``t0``)."""
    dst = dict(dst or {})
    out: dict[str, Any] = {
        "sample_count": int(dst.get("sample_count") or 0) + int(src.get("sample_count") or 0),
        "sample_weight": (sample_weight(dst) if dst else 0) + sample_weight(src),
    }
    for f in _CTX_SCALARS:
        out[f] = _n(dst.get(f)) + _n(src.get(f))
    a, b = list(dst.get("len_hist") or []), list(src.get("len_hist") or [])
    out["len_hist"] = [_n(a[i] if i < len(a) else 0) + _n(b[i] if i < len(b) else 0)
                       for i in range(max(len(a), len(b)))]
    for kf, cf, tf in _CTX_KEYED:
        keys, counts = list(dst.get(kf) or []), [_n(n) for n in dst.get(cf) or []]
        counts += [0] * (len(keys) - len(counts))
        ts = list(dst.get(tf) or []) if texts else []
        for i, k in enumerate(src.get(kf) or []):
            n = _n((src.get(cf) or [])[i]) if i < len(src.get(cf) or []) else 0
            if k in keys:
                counts[keys.index(k)] += n
            else:
                src_t = src.get(tf) or []
                if texts and (i >= len(src_t) or not src_t[i]):
                    continue  # malformed: a key without its ciphertext can't be shown; drop it
                keys.append(k)
                counts.append(n)
                if texts:
                    ts.append(src_t[i])
        out[kf], out[cf] = keys, counts
        if texts:
            out[tf] = ts
    return out


def _n(v: Any) -> Any:
    """A stored count: int or float as is (weights are floats under decay — never truncate)."""
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else (float(v) if v else 0)


def _copy(c: Constraints) -> Constraints:
    return {
        **c,
        "top_openers": list(c["top_openers"]),
        "top_transitions": list(c["top_transitions"]),
        "top_bigrams": [list(x) for x in c["top_bigrams"]],
        "punctuation_rates": dict(c["punctuation_rates"]),
        "structures": [dict(s) for s in c["structures"]],
        "lexicon": list(c["lexicon"]),
    }  # type: ignore[return-value]


def _top_items(d: dict[Any, float], n: int) -> list[tuple[Any, float]]:
    """Top ``n`` by value desc, key asc (keys are all str or all str-tuples — same order as Cypher)."""
    return sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))[:n]


def _top_words(d: dict[str, float], n: int) -> list[tuple[str, float]]:
    """Top ``n`` words by count desc, then blind index asc — Neo4j's ``ORDER BY n DESC, key ASC``."""
    return sorted(d.items(), key=lambda kv: (-kv[1], word_key(kv[0])))[:n]


def _top_bigrams(d: dict[tuple[str, str], float], n: int) -> list[tuple[tuple[str, str], float]]:
    return sorted(d.items(), key=lambda kv: (-kv[1], word_key(kv[0][0]), word_key(kv[0][1])))[:n]


# ============================================================================ in-memory
class InMemoryGraphStore(_BaseStore):
    """Dict-backed store with the same semantics as Neo4jGraphStore (tests / fallback).
    Weights are stored in decay units exactly like Neo4j (``graph/decay.py``)."""

    kind = "memory"

    def __init__(
        self,
        *,
        half_life_days: float = 0.0,
        clock: Callable[[], float] = time.time,
        t0: float | None = None,
        rebase_at: float = REBASE_AT,
    ) -> None:
        decay = Decay(half_life_days, clock, rebase_at=rebase_at)
        decay.t0 = decay.now() if t0 is None else float(t0)
        super().__init__(decay)
        self.fallback = False
        self._lock = threading.RLock()
        self.words: dict[str, float] = defaultdict(int)
        self.word_p: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(int))
        self.bigrams: dict[tuple[str, str], float] = defaultdict(int)
        self.bigram_p: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: defaultdict(int))
        self.succ: dict[str, set[str]] = defaultdict(set)  # FOLLOWED_BY adjacency (fan-out guard)
        self.pred: dict[str, set[str]] = defaultdict(set)
        self.structures: dict[str, float] = defaultdict(int)
        self.struct_ctx: dict[tuple[str, str], float] = defaultdict(int)
        self.word_struct: dict[tuple[str, str], float] = defaultdict(int)
        self.punct: dict[str, float] = defaultdict(int)
        self.punct_ctx: dict[tuple[str, str], float] = defaultdict(int)
        self.contexts: dict[str, dict[str, Any]] = {}

    def ensure_schema(self) -> None:
        pass

    def healthy(self) -> bool:
        return True

    def wipe(self) -> int:
        with self._lock:
            n = len(self.words) + len(self.structures) + len(self.punct) + len(self.contexts)
            for d in (self.words, self.word_p, self.bigrams, self.bigram_p, self.succ, self.pred,
                      self.structures, self.struct_ctx, self.word_struct, self.punct, self.punct_ctx,
                      self.contexts):
                d.clear()
            self.decay.t0 = self.decay.now()  # an empty graph restarts the decay epoch
        self.invalidate()
        return n

    def write_extraction(self, extraction: Extraction | dict[str, Any], persona: str) -> dict[str, int]:
        full = Extraction.from_any(extraction)
        with self._lock:
            ex, d = prepare(full, self.decay.scale())
            ctx = self.contexts.setdefault(
                persona,
                {
                    "name": persona, "sample_count": 0, "sample_weight": 0, "sentence_count": 0,
                    "lower_starts": 0, "upper_starts": 0, "len_sum": 0, "len_hist": [],
                    "contractions": 0, "expansions": 0, "opener_keys": [], "opener_counts": [],
                    "opener_phrase_keys": [], "opener_phrase_counts": [],
                    "transition_keys": [], "transition_counts": [],
                },
            )
            ctx["sample_count"] += 1
            for f in ("sample_weight", "sentence_count", "lower_starts", "upper_starts", "len_sum",
                      "contractions", "expansions"):
                ctx[f] += d[f]
            old = ctx["len_hist"]
            ctx["len_hist"] = [(old[i] if i < len(old) else 0) + n for i, n in enumerate(d["len_hist"])]
            for kf, cf, src in (
                ("opener_keys", "opener_counts", d["openers"]),
                ("opener_phrase_keys", "opener_phrase_counts", d["opener_phrases"]),
                ("transition_keys", "transition_counts", d["transitions"]),
            ):
                for key, n in src.items():
                    if key in ctx[kf]:
                        ctx[cf][ctx[kf].index(key)] += n
                    else:
                        ctx[kf].append(key)
                        ctx[cf].append(n)
            for w, n in ex.words.items():
                self.words[w] += n
                self.word_p[w][persona] += n
            for bg, n in ex.bigrams.items():
                if bg[0] in self.words and bg[1] in self.words:  # mirrors MATCH semantics
                    self.bigrams[bg] += n
                    self.bigram_p[bg][persona] += n
                    self.succ[bg[0]].add(bg[1])
                    self.pred[bg[1]].add(bg[0])
            self._prune_fanout({a for a, _ in ex.bigrams}, {b for _, b in ex.bigrams})
            for sig, n in ex.structures.items():
                self.structures[sig] += n
                self.struct_ctx[(sig, persona)] += n
            for (w, sig), n in ex.word_structures.items():
                if w in self.words and sig in self.structures:
                    self.word_struct[(w, sig)] += n
            for sym, n in ex.punctuation.items():
                self.punct[sym] += n
                self.punct_ctx[(sym, persona)] += n
        self.invalidate()
        return {"words_merged": len(ex.words), "sentences": full.sentence_count}

    def _prune_fanout(self, firsts: set[str], seconds: set[str], cap: int = GRAPH_FANOUT) -> None:
        """Mirror of PRUNE_FOLLOWERS then PRUNE_PREDECESSORS (caller holds the lock)."""
        for adj, words, other in ((self.succ, firsts, 1), (self.pred, seconds, 0)):
            for w in sorted(words):
                if len(adj.get(w, ())) <= cap:
                    continue
                edges = [(w, x) if other == 1 else (x, w) for x in adj[w]]
                edges.sort(key=lambda bg: (-self.bigrams[bg], word_key(bg[other])))
                for bg in edges[cap:]:
                    self.bigrams.pop(bg, None)
                    self.bigram_p.pop(bg, None)
                    self.succ[bg[0]].discard(bg[1])
                    self.pred[bg[1]].discard(bg[0])

    def merge_persona(self, src: str, dst: str) -> int:
        if src == dst:
            return 0
        with self._lock:
            ctx = self.contexts.pop(src, None)
            if ctx is None:
                return 0
            merged = merged_context(self.contexts.get(dst), ctx, texts=False)
            self.contexts[dst] = {"name": dst, **merged}
            for per in (self.word_p, self.bigram_p):
                for p in per.values():
                    if src in p:
                        p[dst] += p.pop(src)
            for edges in (self.struct_ctx, self.punct_ctx):
                for (x, c) in [key for key in edges if key[1] == src]:
                    edges[(x, dst)] += edges.pop((x, c))
        self.invalidate()
        return int(ctx.get("sample_count") or 0)

    def _aggregate(self, persona: str | None, k: int) -> StyleAggregate:
        with self._lock:
            return self._stored_aggregate(persona, k).rescale(self.decay.scale())

    def _stored_aggregate(self, persona: str | None, k: int) -> StyleAggregate:
        """The aggregate in stored (decay) units, ranked on them like the Cypher reads."""
        agg = StyleAggregate()
        with self._lock:
            for name, ctx in self.contexts.items():
                if persona is None or name == persona:
                    agg.merge_context(ctx)
            if persona is None:
                words = dict(self.words)
                bigrams = dict(self.bigrams)
            else:
                words = {w: p[persona] for w, p in self.word_p.items() if p.get(persona)}
                bigrams = {bg: p[persona] for bg, p in self.bigram_p.items() if p.get(persona)}
            top = _top_words(words, _WORD_FETCH)
            agg.words = dict(top)
            agg.word_totals = {w: self.words[w] for w in agg.words}
            seed = {w for w, _ in top[:_BIGRAM_SEED]}
            agg.bigrams = dict(_top_bigrams({bg: n for bg, n in bigrams.items() if bg[0] in seed}, k))
            structs: dict[str, int] = defaultdict(int)
            for (sig, ctx), n in self.struct_ctx.items():
                if persona is None or ctx == persona:
                    structs[sig] += n
            agg.structures = dict(_top_items(structs, max(k, _STRUCT_FETCH)))
            punct: dict[str, int] = defaultdict(int)
            for (sym, ctx), n in self.punct_ctx.items():
                if persona is None or ctx == persona:
                    punct[sym] += n
            agg.punctuation = dict(punct)
        return agg

    def _top_nodes(self, persona: str | None, n_words: int, n_structs: int) -> dict[str, list]:
        agg = self._stored_aggregate(persona, n_structs)  # get_graph divides by s(now)
        with self._lock:
            if persona is None:
                words = dict(self.words)
            else:
                words = {w: p[persona] for w, p in self.word_p.items() if p.get(persona)}
            ctxs = [
                (n, c["sample_count"]) for n, c in self.contexts.items() if persona is None or n == persona
            ]
        return {
            "words": _top_words(words, n_words),
            "structures": _top_items(agg.structures, n_structs),
            "punctuation": _top_items(agg.punctuation, 8),
            "contexts": sorted(ctxs, key=lambda x: (-x[1], x[0])),
        }

    def _edges_among(self, persona, words, sigs, syms, ctxs) -> dict[str, list]:
        ws, ss, ps, cs = set(words), set(sigs), set(syms), set(ctxs)
        with self._lock:
            if persona is None:
                followed = [(a, b, n) for (a, b), n in self.bigrams.items() if a in ws and b in ws]
            else:
                followed = [
                    (a, b, p[persona])
                    for (a, b), p in self.bigram_p.items()
                    if a in ws and b in ws and p.get(persona)
                ]
            part_of = [(w, s, n) for (w, s), n in self.word_struct.items() if w in ws and s in ss]
            used_in = [("s", s, c, n) for (s, c), n in self.struct_ctx.items() if s in ss and c in cs]
            used_in += [("p", p, c, n) for (p, c), n in self.punct_ctx.items() if p in ps and c in cs]
        return {"followed": followed, "part_of": part_of, "used_in": used_in}

    def stats(self) -> dict[str, Any]:
        with self._lock:
            sentences = sum(c["sentence_count"] for c in self.contexts.values())
            len_sum = sum(c["len_sum"] for c in self.contexts.values())
            return {
                "words": len(self.words),
                "structures": len(self.structures),
                "samples": sum(c["sample_count"] for c in self.contexts.values()),
                "avg_sentence_len": round(len_sum / sentences, 2) if sentences else None,
            }

    # -- recency decay ---------------------------------------------------------------
    def decay_maintenance(self) -> dict[str, int]:
        if not self.decay.enabled:
            return {}
        with self._lock:
            now = self.decay.now()
            out = {"rebased": 0}
            if self.decay.scale(now) > self.decay.rebase_at:
                self._rebase(rebase_factor(self.decay.t0 or now, self.decay.half_life_s, now), now)
                out["rebased"] = 1
            out.update(self._prune(PRUNE_BELOW * self.decay.scale(now)))
        self.invalidate()
        return out

    def _rebase(self, f: float, now: float) -> None:
        """Multiply every decayed value by ``f`` and move ``t0`` to ``now`` (caller holds the
        lock). Mirrors the Cypher ``REBASE`` queries value for value."""
        for flat in (self.words, self.bigrams, self.structures, self.struct_ctx, self.word_struct,
                     self.punct, self.punct_ctx):
            for key in flat:
                flat[key] = flat[key] * f
        for nested in (self.word_p, self.bigram_p):
            for per in nested.values():
                for p in per:
                    per[p] = per[p] * f
        for ctx in self.contexts.values():
            scale_context(ctx, f)
        self.decay.t0 = now

    def _prune(self, below: float) -> dict[str, int]:
        """Mirror of ``Q.PRUNE``: faded edges, then faded words with no edge left."""
        faded = [bg for bg, w in self.bigrams.items() if w < below]
        for bg in faded:
            self.bigrams.pop(bg, None)
            self.bigram_p.pop(bg, None)
            self.succ[bg[0]].discard(bg[1])
            self.pred[bg[1]].discard(bg[0])
        faded_ws = [k for k, w in self.word_struct.items() if w < below]
        for k in faded_ws:
            del self.word_struct[k]
        linked = {w for w, _ in self.word_struct}
        gone = [w for w, n in self.words.items()
                if n < below and not self.succ.get(w) and not self.pred.get(w) and w not in linked]
        for w in gone:
            del self.words[w]
            self.word_p.pop(w, None)
            self.succ.pop(w, None)
            self.pred.pop(w, None)
        return {"followed_by": len(faded), "part_of": len(faded_ws), "words": len(gone)}


# ============================================================================ neo4j
class Neo4jGraphStore(_BaseStore):
    kind = "neo4j"

    def __init__(
        self,
        uri: str,
        user: str,
        password: str,
        *,
        timeout: float = 3.0,
        half_life_days: float = 0.0,
        clock: Callable[[], float] = time.time,
        rebase_at: float = REBASE_AT,
    ):
        super().__init__(Decay(half_life_days, clock, rebase_at=rebase_at))  # t0: init_decay()
        self._rebase_lock = RebaseLock()
        self._decay_init_lock = threading.Lock()
        from neo4j import GraphDatabase

        self._driver = GraphDatabase.driver(
            uri,
            auth=(user, password),
            connection_timeout=timeout,
            connection_acquisition_timeout=timeout,
            max_transaction_retry_time=timeout,
        )
        self._plain: dict[str, str] = {}  # blind index -> decrypted word / opener / transition
        self._plain_lock = threading.Lock()
        self.migration: dict[str, int] = {}
        self.decay_batch = DECAY_BATCH

    # -- encryption helpers ---------------------------------------------------------
    def _learn(self, key: str, plain: str) -> None:
        with self._plain_lock:
            self._plain[key] = plain

    def _open(self, key: str, token: str | None, ctx: str) -> str:
        """Plaintext for ``key`` (memoized); decrypts ``token`` on the first sight of a key."""
        hit = self._plain.get(key)
        if hit is not None:
            return hit
        plain = crypto.decrypt(token, ctx) if token is not None else ""
        self._learn(key, plain)
        return plain

    def _word_rows(self, words: dict[str, int]) -> list[dict[str, Any]]:
        out = []
        for w, n in words.items():
            k = word_key(w)
            self._learn(k, w)
            out.append({"key": k, "text": crypto.encrypt(w, WORD_CTX), "n": n})
        return out

    def _keyed_delta(self, d: dict[str, Any]) -> dict[str, Any]:
        d = dict(d)
        for field_, keys_p, texts_p in CTX_LISTS:
            rows = []
            for value, n in d[field_].items():
                k = list_key(keys_p, value)
                self._learn(k, value)
                rows.append({"k": k, "e": crypto.encrypt(value, f"neo4j:IntentContext.{texts_p}"), "n": n})
            d[field_] = rows
        return d

    def _plain_context(self, c: dict[str, Any]) -> dict[str, Any]:
        """IntentContext properties with the opener / transition keys replaced by plaintext."""
        c = dict(c)
        for _, keys_p, texts_p in CTX_LISTS:
            keys = c.get(keys_p) or []
            texts = c.get(texts_p)
            if texts is None:  # legacy plaintext context (migrated at start-up)
                continue
            ctx = f"neo4j:IntentContext.{texts_p}"
            c[keys_p] = [self._open(k, t, ctx) for k, t in zip(keys, texts, strict=False)]
        return c

    @classmethod
    def from_settings(
        cls, settings: Any, timeout: float = 3.0, clock: Callable[[], float] = time.time
    ) -> Neo4jGraphStore:
        return cls(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password, timeout=timeout,
                   half_life_days=decay_half_life_days(settings), clock=clock)

    def close(self) -> None:
        self._driver.close()

    def healthy(self) -> bool:
        try:
            self._driver.verify_connectivity()
            return True
        except Exception:  # noqa: BLE001 - any driver failure means unhealthy
            return False

    def _read(self, query: str, **params: Any) -> list[dict[str, Any]]:
        records, _, _ = self._driver.execute_query(query, params, routing_="r")
        return [r.data() for r in records]

    def execute_read(self, query: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Run an arbitrary read-only Cypher query (used by the graph MCP server)."""
        with self._driver.session(default_access_mode="READ") as s:
            return s.execute_read(lambda tx: [r.data() for r in tx.run(query, params or {})])

    def ensure_schema(self) -> None:
        for stmt in Q.SCHEMA:
            self._driver.execute_query(stmt)

    def write_extraction(self, extraction: Extraction | dict[str, Any], persona: str) -> dict[str, int]:
        full = Extraction.from_any(extraction)
        self._ensure_decay()
        try:
            with self._rebase_lock.shared():  # increments are in the units of the current t0
                ex = self._write(full, persona)
        finally:
            self.invalidate()
        return {"words_merged": len(ex.words), "sentences": full.sentence_count}

    def _write(self, full: Extraction, persona: str) -> Extraction:
        ex, delta = prepare(full, self.decay.scale())
        d = self._keyed_delta(delta)
        words = self._word_rows(ex.words)
        bigrams = [{"a": word_key(a), "b": word_key(b), "n": n} for (a, b), n in ex.bigrams.items()]
        structs = [{"sig": s, "n": n} for s, n in ex.structures.items()]
        wstructs = [{"w": word_key(w), "sig": s, "n": n} for (w, s), n in ex.word_structures.items()]
        puncts = [{"sym": s, "n": n} for s, n in ex.punctuation.items()]
        # sorted like the in-memory mirror (first words, then second words, key order)
        firsts = sorted({word_key(a) for a, _ in ex.bigrams})
        seconds = sorted({word_key(b) for _, b in ex.bigrams})

        def work(tx: Any) -> None:
            tx.run(Q.MERGE_CONTEXT, persona=persona, d=d).consume()
            for rows in chunks(words):
                tx.run(Q.MERGE_WORDS, rows=rows, persona=persona).consume()
            for rows in chunks(bigrams):
                tx.run(Q.MERGE_BIGRAMS, rows=rows, persona=persona).consume()
            if bigrams:
                for query, keys in ((Q.PRUNE_FOLLOWERS, firsts), (Q.PRUNE_PREDECESSORS, seconds)):
                    for ks in chunks(keys):
                        tx.run(query, keys=ks, cap=GRAPH_FANOUT).consume()
            for rows in chunks(structs):
                tx.run(Q.MERGE_STRUCTURES, rows=rows, persona=persona).consume()
            for rows in chunks(wstructs):
                tx.run(Q.MERGE_WORD_STRUCTURES, rows=rows).consume()
            if puncts:
                tx.run(Q.MERGE_PUNCTUATION, rows=puncts, persona=persona).consume()

        with self._driver.session() as s:
            s.execute_write(work)
        return ex

    def _aggregate(self, persona: str | None, k: int) -> StyleAggregate:
        """All reads for one aggregate in ONE read transaction (one session, one BEGIN/COMMIT,
        a consistent view) instead of five auto-commit queries. Ranked on the stored weights
        (like the in-memory store), then divided by ``s(now)``."""
        self._ensure_decay()
        with self._rebase_lock.shared():
            s = self.decay.scale()
            return self._stored_aggregate(persona, k).rescale(s)

    def _stored_aggregate(self, persona: str | None, k: int) -> StyleAggregate:
        """The aggregate in stored (decay) units."""

        def rows(tx: Any, query: str, **params: Any) -> list[dict[str, Any]]:
            return [r.data() for r in tx.run(query, persona=persona, **params)]

        def work(tx: Any) -> StyleAggregate:
            agg = StyleAggregate()
            for row in rows(tx, Q.READ_CONTEXTS):
                agg.merge_context(self._plain_context(row["c"]))
            if agg.sample_count == 0:
                return agg
            words = rows(tx, Q.READ_TOP_WORDS, limit=_WORD_FETCH)
            for r in words:
                r["w"] = self._open(r["key"], r["text"], WORD_CTX)
            agg.words = {r["w"]: _n(r["n"]) for r in words}
            agg.word_totals = {r["w"]: _n(r["total"]) for r in words}
            seed = [r["key"] for r in words[:_BIGRAM_SEED]]
            agg.bigrams = {
                (self._open(r["ak"], r["a"], WORD_CTX), self._open(r["bk"], r["b"], WORD_CTX)): _n(r["n"])
                for r in rows(tx, Q.READ_TOP_BIGRAMS, seed=seed, limit=k)
            }
            agg.structures = {
                r["sig"]: _n(r["n"]) for r in rows(tx, Q.READ_STRUCTURES, limit=max(k, _STRUCT_FETCH))
            }
            agg.punctuation = {r["sym"]: _n(r["n"]) for r in rows(tx, Q.READ_PUNCTUATION)}
            return agg

        with self._driver.session(default_access_mode="READ") as s:
            return s.execute_read(work)

    def _top_nodes(self, persona: str | None, n_words: int, n_structs: int) -> dict[str, list]:
        with self._rebase_lock.shared():
            return self._stored_top_nodes(persona, n_words, n_structs)

    def _stored_top_nodes(self, persona: str | None, n_words: int, n_structs: int) -> dict[str, list]:
        ctxs = [
            (r["c"]["name"], int(r["c"].get("sample_count") or 0))
            for r in self._read(Q.READ_CONTEXTS, persona=persona)
        ]
        return {
            "words": [
                (self._open(r["key"], r["text"], WORD_CTX), r["n"] or 0)
                for r in self._read(Q.READ_TOP_WORDS, persona=persona, limit=n_words)
            ],
            "structures": [
                (r["sig"], r["n"] or 0) for r in self._read(Q.READ_STRUCTURES, persona=persona, limit=n_structs)
            ],
            "punctuation": _top_items(
                {r["sym"]: r["n"] or 0 for r in self._read(Q.READ_PUNCTUATION, persona=persona)}, 8
            ),
            "contexts": sorted(ctxs, key=lambda x: (-x[1], x[0])),
        }

    def _edges_among(self, persona, words, sigs, syms, ctxs) -> dict[str, list]:
        with self._rebase_lock.shared():
            return self._stored_edges_among(persona, words, sigs, syms, ctxs)

    def _stored_edges_among(self, persona, words, sigs, syms, ctxs) -> dict[str, list]:
        by_key = {word_key(w): w for w in words}
        keys = list(by_key)
        followed = [
            (by_key[r["a"]], by_key[r["b"]], r["n"] or 0)
            for r in self._read(Q.READ_FOLLOWED_AMONG, words=keys, persona=persona)
            if r["a"] in by_key and r["b"] in by_key
        ]
        part_of = [
            (by_key[r["w"]], r["sig"], r["n"] or 0)
            for r in self._read(Q.READ_PART_OF_AMONG, words=keys, sigs=sigs)
            if r["w"] in by_key
        ]
        used_in = [
            (r["kind"], r["key"], r["ctx"], r["n"] or 0)
            for r in self._read(Q.READ_USED_IN_AMONG, ctxs=ctxs, sigs=sigs, syms=syms)
        ]
        return {"followed": followed, "part_of": part_of, "used_in": used_in}

    def merge_persona(self, src: str, dst: str) -> int:
        if src == dst:
            return 0

        def work(tx: Any) -> int:
            rec = tx.run(Q.READ_CONTEXT_PAIR, src=src, dst=dst).single()
            if rec is None or rec["s"] is None:
                return 0
            merged = merged_context(rec["d"], rec["s"], texts=True)
            tx.run(Q.MOVE_PERSONA_WEIGHTS, src=src, dst=dst).consume()
            tx.run(Q.MOVE_PERSONA_USED_IN, src=src, dst=dst).consume()
            tx.run(Q.SET_PERSONA_CONTEXT, src=src, dst=dst, props=merged).consume()
            return int(rec["s"].get("sample_count") or 0)

        try:
            with self._rebase_lock.shared(), self._driver.session() as s:
                return s.execute_write(work)
        finally:
            self.invalidate()

    def wipe(self) -> int:
        total = 0
        with self._rebase_lock.exclusive():
            while True:
                records, _, _ = self._driver.execute_query(Q.WIPE_BATCH, {"limit": 5000})
                n = int(records[0]["n"]) if records else 0
                total += n
                if n == 0:
                    break
            if self.decay.enabled:  # nothing left in old units: restart the epoch now
                now = self.decay.now()
                self._driver.execute_query(Q.RESET_DECAY, {"now": now, "h": self.decay.half_life_s})
                self.decay.t0 = now
        with self._plain_lock:
            self._plain.clear()
        self.invalidate()
        return total

    # -- recency decay ---------------------------------------------------------------
    def _read_scale(self) -> float:
        self._ensure_decay()
        return self.decay.scale()

    def _ensure_decay(self) -> None:
        """Load ``t0`` on first use (only needed when decay is on)."""
        if self.decay.enabled and self.decay.t0 is None:
            with self._decay_init_lock:
                if self.decay.t0 is None:
                    self._init_decay()

    def init_decay(self) -> dict[str, int]:
        """Start-up (``connect_graph_store``): finish a rebase a crash interrupted, record ``t0``
        on first use, and rebase when the half-life changed since the last run (the stored units
        were scaled with the old one; switching decay off / on is a change too). Returns counts."""
        with self._decay_init_lock:
            return self._init_decay()

    def _init_decay(self) -> dict[str, int]:
        out: dict[str, int] = {}
        h = self.decay.half_life_s
        st = self._meta()
        if st.get("rebase_target_t0") is not None:
            self._rebase(self.decay.now())  # resume: the recorded target / factor win
            out["resumed"] = 1
            st = self._meta()
        if st.get("decay_t0") is None:
            if h > 0:
                records, _, _ = self._driver.execute_query(Q.INIT_DECAY, {"now": self.decay.now(), "h": h})
                self.decay.t0 = float(records[0]["m"]["decay_t0"])
        elif float(st.get("decay_half_life_s") or 0) != h:
            self._rebase(self.decay.now(), new_h=h)
            out["rebased"] = 1
        else:
            self.decay.t0 = float(st["decay_t0"])
        if out:
            log.info("graph decay epoch: %s", out)
        return out

    def _rebase(self, now: float, new_h: float | None = None) -> None:
        """Multiply every decayed value by ``1 / s(now)`` (computed from the half-life the data
        was written with) and move ``t0`` to ``now`` — or finish a pending rebase. Crash-safe and
        never double-applied (protocol in ``queries.py``). Blocks this store's reads and writes."""
        target_h = self.decay.half_life_s if new_h is None else new_h
        with self._rebase_lock.exclusive():

            def begin(tx: Any) -> dict[str, Any]:
                m = tx.run(Q.LOCK_META).single()["m"]
                if m.get("rebase_target_t0") is not None:  # interrupted earlier: finish that one
                    return {"epoch": int(m["rebase_epoch"]), "factor": float(m["rebase_factor"])}
                t0 = float(m["decay_t0"]) if m.get("decay_t0") is not None else now
                f = rebase_factor(t0, float(m.get("decay_half_life_s") or 0), now)
                epoch = int(m.get("rebase_epoch") or 0) + 1
                tx.run(Q.BEGIN_REBASE, epoch=epoch, target_t0=now, factor=f, target_h=target_h).consume()
                return {"epoch": epoch, "factor": f}

            with self._driver.session() as s:
                st = s.execute_write(begin)
            if st["factor"] != 1.0:
                for _, query in Q.REBASE:
                    self._run_batched(query, epoch=st["epoch"], factor=st["factor"])
            self._driver.execute_query(Q.FINISH_REBASE, {"epoch": st["epoch"]})
            t0 = self._meta().get("decay_t0")
            self.decay.t0 = float(t0) if t0 is not None else None
        self.invalidate()

    def _meta(self) -> dict[str, Any]:
        """``PolytoneMeta {name:'schema'}`` properties ({} when there is none yet)."""
        records, _, _ = self._driver.execute_query(Q.META_DECAY, routing_="r")
        return dict(records[0]["m"]) if records else {}

    def _run_batched(self, query: str, **params: Any) -> int:
        """Run a ``CALL {…} IN TRANSACTIONS`` query (auto-commit session); its ``n`` if any."""
        with self._driver.session() as s:
            rows = list(s.run(query.replace("__BATCH__", str(int(self.decay_batch))), params))
        return int(rows[0].data().get("n") or 0) if rows else 0

    def decay_maintenance(self) -> dict[str, int]:
        if not self.decay.enabled:
            return {}
        self._ensure_decay()
        now = self.decay.now()
        out = {"rebased": 0}
        if self.decay.scale(now) > self.decay.rebase_at:
            self._rebase(now)
            out["rebased"] = 1
        with self._rebase_lock.shared():  # "below" is in the units of the current t0
            below = PRUNE_BELOW * self.decay.scale(now)
            for key, query in Q.PRUNE:
                out[key] = self._run_batched(query, below=below)
        self.invalidate()
        return out

    def migrate_encryption(self, batch: int = MIGRATE_BATCH) -> dict[str, int]:
        """Encrypt a legacy plaintext graph in place (see ``queries.py``). Safe on a live graph:
        batched (one write transaction per ``batch`` nodes), resumable (only unmigrated nodes are
        selected; the marker is written last) and idempotent (a migrated graph costs one read)."""
        records, _, _ = self._driver.execute_query(Q.META_ENC_VERSION, routing_="r")
        if records and (records[0]["v"] or 0) >= 1:
            return {}
        counts = {"words": 0, "contexts": 0}
        while True:
            records, _, _ = self._driver.execute_query(Q.MIGRATE_WORDS_BATCH, {"limit": batch}, routing_="r")
            rows = []
            for r in records:
                text = r["text"]
                if crypto.is_encrypted(text):  # never double-encrypt (cannot happen without a key)
                    continue
                k = word_key(text)
                self._learn(k, text)
                rows.append({"id": r["id"], "key": k, "enc": crypto.encrypt(text, WORD_CTX)})
            if not rows:
                break
            res, _, _ = self._driver.execute_query(Q.MIGRATE_WORDS_APPLY, {"rows": rows})
            counts["words"] += int(res[0]["n"]) if res else 0
        while True:
            records, _, _ = self._driver.execute_query(Q.MIGRATE_CONTEXTS_BATCH, {"limit": batch}, routing_="r")
            if not records:
                break
            rows = []
            for r in records:
                row: dict[str, Any] = {"id": r["id"]}
                for src, short, (_, keys_p, texts_p) in zip(
                    ("openers", "phrases", "transitions"), ("opener", "phrase", "transition"), CTX_LISTS, strict=True
                ):
                    plain = [str(x) for x in (r[src] or [])]
                    row[f"{short}_keys"] = [list_key(keys_p, v) for v in plain]
                    row[f"{short}_texts"] = [crypto.encrypt(v, f"neo4j:IntentContext.{texts_p}") for v in plain]
                rows.append(row)
            res, _, _ = self._driver.execute_query(Q.MIGRATE_CONTEXTS_APPLY, {"rows": rows})
            counts["contexts"] += int(res[0]["n"]) if res else 0
        self._driver.execute_query(Q.DROP_LEGACY_WORD_CONSTRAINT)
        self._driver.execute_query(Q.SET_META_ENC_VERSION, {"v": 1})
        self.invalidate()
        if counts["words"] or counts["contexts"]:
            log.info("encrypted the style graph at rest: %s", counts)
        return {k: v for k, v in counts.items() if v}

    def stats(self) -> dict[str, Any]:
        r = self._read(Q.STATS)[0]
        sentences = _n(r["sentences"])  # decayed (float): the average is recency-weighted
        return {
            "words": int(r["words"]),
            "structures": int(r["structures"]),
            "samples": int(r["samples"] or 0),
            "avg_sentence_len": round(_n(r["len_sum"]) / sentences, 2) if sentences else None,
        }


# ============================================================================ factory
def decay_half_life_days(settings: Any) -> float:
    """``POLYTONE_DECAY_HALF_LIFE_DAYS`` (0 / negative / invalid = decay off)."""
    try:
        return max(0.0, float(getattr(settings, "polytone_decay_half_life_days", 0.0) or 0.0))
    except (TypeError, ValueError):
        return 0.0


def connect_graph_store(settings: Any, timeout: float = 2.0) -> GraphStore:
    """Neo4j if reachable (schema ensured), else InMemoryGraphStore with a warning."""
    store: Neo4jGraphStore | None = None
    try:
        store = Neo4jGraphStore.from_settings(settings, timeout=timeout)
        store._driver.verify_connectivity()
        store.ensure_schema()
        store.migration = store.migrate_encryption()
        store.init_decay()
        return store
    except Exception as e:  # noqa: BLE001
        if store is not None:
            store.close()
        log.warning("Neo4j unavailable at %s (%s); using in-memory graph store", settings.neo4j_uri, e)
        mem = InMemoryGraphStore(half_life_days=decay_half_life_days(settings))
        mem.fallback = True  # stands in for an unreachable Neo4j (persona deletes refuse to run)
        return mem
