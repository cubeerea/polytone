"""Pure functions turning raw per-persona graph aggregates into a ``Constraints`` dict.

Both ``Neo4jGraphStore`` and ``InMemoryGraphStore`` fetch a ``StyleAggregate`` and run
it through ``compute_constraints`` so their semantics are identical by construction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TypedDict

from backend.ingestion.text import EXPANDED_FORMS, FUNCTION_WORDS, is_contraction
from backend.ingestion.types import Extraction

LEN_HIST_SIZE = 61  # sentence-length histogram buckets 0..60 (60 = "60 or more")


class Constraints(TypedDict):
    avg_sentence_len: float
    sentence_len_p90: float
    top_openers: list[str]
    top_transitions: list[str]
    top_bigrams: list[list[Any]]  # [word_a, word_b, weight]
    punctuation_rates: dict[str, float]  # occurrences per sentence
    structures: list[dict[str, Any]]  # {"signature": str, "weight": float (share 0-1)}
    lexicon: list[str]
    casing: str  # "lower" | "sentence" | "mixed"
    contractions_rate: float  # contractions / (contractions + expandable forms)
    sample_count: int


def empty_constraints() -> Constraints:
    return {
        "avg_sentence_len": 0.0,
        "sentence_len_p90": 0.0,
        "top_openers": [],
        "top_transitions": [],
        "top_bigrams": [],
        "punctuation_rates": {},
        "structures": [],
        "lexicon": [],
        "casing": "sentence",
        "contractions_rate": 0.0,
        "sample_count": 0,
    }


@dataclass
class StyleAggregate:
    """Raw counts for one persona (or all personas merged)."""

    sample_count: int = 0
    sentence_count: int = 0
    lower_starts: int = 0
    upper_starts: int = 0
    len_sum: int = 0
    len_hist: list[int] = field(default_factory=lambda: [0] * LEN_HIST_SIZE)
    contractions: int = 0
    expansions: int = 0
    openers: dict[str, int] = field(default_factory=dict)
    transitions: dict[str, int] = field(default_factory=dict)
    words: dict[str, int] = field(default_factory=dict)  # top words by count
    bigrams: dict[tuple[str, str], int] = field(default_factory=dict)  # top bigrams
    structures: dict[str, int] = field(default_factory=dict)
    punctuation: dict[str, int] = field(default_factory=dict)

    def merge_context(self, ctx: dict[str, Any]) -> None:
        """Add an IntentContext node's aggregate properties (Neo4j/in-memory shape)."""
        self.sample_count += int(ctx.get("sample_count") or 0)
        self.sentence_count += int(ctx.get("sentence_count") or 0)
        self.lower_starts += int(ctx.get("lower_starts") or 0)
        self.upper_starts += int(ctx.get("upper_starts") or 0)
        self.len_sum += int(ctx.get("len_sum") or 0)
        self.contractions += int(ctx.get("contractions") or 0)
        self.expansions += int(ctx.get("expansions") or 0)
        for i, n in enumerate(ctx.get("len_hist") or []):
            if i < LEN_HIST_SIZE:
                self.len_hist[i] += int(n or 0)
        for keys_f, counts_f, target in (
            ("opener_keys", "opener_counts", self.openers),
            ("transition_keys", "transition_counts", self.transitions),
        ):
            for k, n in zip(ctx.get(keys_f) or [], ctx.get(counts_f) or []):
                target[k] = target.get(k, 0) + int(n or 0)


def context_delta(ex: Extraction) -> dict[str, Any]:
    """Per-sample increments applied to the persona's IntentContext aggregate."""
    hist = [0] * LEN_HIST_SIZE
    for n in ex.sentence_lengths:
        hist[min(max(int(n), 0), LEN_HIST_SIZE - 1)] += 1
    return {
        "sentence_count": len(ex.sentence_lengths),
        "lower_starts": int(ex.lower_starts),
        "upper_starts": int(ex.upper_starts),
        "len_sum": int(sum(ex.sentence_lengths)),
        "len_hist": hist,
        "contractions": sum(n for w, n in ex.words.items() if is_contraction(w)),
        "expansions": sum(n for bg, n in ex.bigrams.items() if bg in EXPANDED_FORMS),
        "openers": dict(ex.openers),
        "transitions": dict(ex.transitions),
    }


def _top(d: dict[Any, int], k: int) -> list[Any]:
    return [key for key, _ in sorted(d.items(), key=lambda kv: (-kv[1], str(kv[0])))[:k]]


def _p90(hist: list[int], total: int) -> float:
    if total <= 0:
        return 0.0
    target = 0.9 * total
    acc = 0
    for i, n in enumerate(hist):
        acc += n
        if acc >= target:
            return float(i)
    return float(len(hist) - 1)


def compute_constraints(agg: StyleAggregate, k: int = 20) -> Constraints:
    c = empty_constraints()
    c["sample_count"] = int(agg.sample_count)
    if agg.sample_count <= 0 and agg.sentence_count <= 0:
        return c
    sc = agg.sentence_count
    if sc > 0:
        c["avg_sentence_len"] = round(agg.len_sum / sc, 2)
        c["sentence_len_p90"] = _p90(agg.len_hist, sc)
        lower_share = agg.lower_starts / sc
        c["casing"] = "lower" if lower_share >= 0.6 else "sentence" if lower_share <= 0.2 else "mixed"
        c["punctuation_rates"] = {
            sym: round(n / sc, 3) for sym, n in sorted(agg.punctuation.items()) if n > 0
        }
    c["top_openers"] = _top(agg.openers, min(k, 8))
    c["top_transitions"] = _top(agg.transitions, min(k, 8))
    c["top_bigrams"] = [
        [a, b, float(n)] for (a, b), n in sorted(agg.bigrams.items(), key=lambda kv: (-kv[1], kv[0]))[:k]
    ]
    total_struct = sum(agg.structures.values())
    if total_struct:
        c["structures"] = [
            {"signature": s, "weight": round(agg.structures[s] / total_struct, 4)}
            for s in _top(agg.structures, k)
        ]
    content = {
        w: n
        for w, n in agg.words.items()
        if w not in FUNCTION_WORDS and len(w) > 2 and not w.isdigit() and "'" not in w
    }
    c["lexicon"] = _top(content, k)
    opp = agg.contractions + agg.expansions
    c["contractions_rate"] = round(agg.contractions / opp, 3) if opp else 0.0
    return c
