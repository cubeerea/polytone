"""Pure functions turning raw graph aggregates into a ``Constraints`` dict.

Both ``Neo4jGraphStore`` and ``InMemoryGraphStore`` fetch ``StyleAggregate``s (one for the
persona, one for all personas) and run them through ``compute_constraints``, so their
semantics are identical by construction.

Persona / global blend
----------------------
A persona with few samples gives noisy statistics, so its cadence is shrunk towards the
user's overall style (all personas) with weight::

    alpha = n_p / (n_p + BLEND_PRIOR)        # n_p = persona sample count, BLEND_PRIOR = 3

  n_p:    0     1     3     6     10    30
  alpha:  0   0.25  0.50  0.67  0.77  0.91

(``alpha = 1`` when the persona is the only one with data; ``alpha = 0`` — pure global — when
the persona has no samples, and then ``sample_count`` reports the global count.)

- Scalars (avg / p90 sentence length, lowercase-start share → casing, contraction rate,
  punctuation per sentence): ``alpha * persona + (1 - alpha) * global``.
- Ranked cadence lists (openers, transitions, structures): items ranked by the blended
  per-sentence share ``alpha * p_count/p_sentences + (1 - alpha) * g_count/g_sentences``.
  ``structures[].weight`` is that blended share.
- Absence is evidence: once the persona has ``>= BLEND_PRIOR`` samples, list items and
  punctuation marks it has never used (global-only) are dropped, so a Boss persona that never
  uses emoji does not inherit the Friend persona's emoji rate.
- Openers / transitions need ``>= MIN_LIST_COUNT`` (2) occurrences in the data that backs them
  once the persona has ``>= 5`` samples (one-off sentence starts are noise); same min count
  for lexicon words and bigrams.
- Vocabulary (lexicon, bigrams) is relationship-specific, so it is persona-only:
  ``top_bigrams`` are the persona's bigrams by persona weight; ``lexicon`` ranks the persona's
  non-generic words by ``count * log2(1 + ratio)`` where ``ratio`` is the word's persona
  frequency over its global frequency (distinctive for this persona, not generic English).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, TypedDict

from backend.ingestion.text import (
    EXPANDED_FORMS,
    FUNCTION_WORDS,
    GENERIC_WORDS,
    is_contraction,
)
from backend.ingestion.types import Extraction

LEN_HIST_SIZE = 61  # sentence-length histogram buckets 0..60 (60 = "60 or more")
BLEND_PRIOR = 3.0
MIN_LIST_COUNT = 2
INFORMAL_SHORT = frozenset({"u", "k", "r", "y", "n"})  # single letters that are style markers
OPENER_PHRASE_SHARE = 0.6  # a 2-3 word opener replaces its first word when it covers ≥60% of it


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
    opener_phrases: dict[str, int] = field(default_factory=dict)
    transitions: dict[str, int] = field(default_factory=dict)
    words: dict[str, int] = field(default_factory=dict)  # top words by (persona) count
    word_totals: dict[str, int] = field(default_factory=dict)  # global count of those words
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
            ("opener_phrase_keys", "opener_phrase_counts", self.opener_phrases),
            ("transition_keys", "transition_counts", self.transitions),
        ):
            for k, n in zip(ctx.get(keys_f) or [], ctx.get(counts_f) or [], strict=False):
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
        "opener_phrases": dict(ex.opener_ngrams),
        "transitions": dict(ex.transitions),
    }


def blend_weight(persona_samples: int, global_samples: int) -> float:
    """Weight of the persona's own statistics (see module docstring)."""
    if persona_samples <= 0:
        return 0.0
    if global_samples <= persona_samples:
        return 1.0
    return persona_samples / (persona_samples + BLEND_PRIOR)


def _ranked(scores: dict[Any, float], k: int) -> list[Any]:
    return [key for key, _ in sorted(scores.items(), key=lambda kv: (-kv[1], str(kv[0])))[:k]]


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


def _shares(d: dict[Any, int], total: int) -> dict[Any, float]:
    return {key: n / total for key, n in d.items() if n > 0} if total > 0 else {}


def _blend_dicts(
    p: dict[Any, float], g: dict[Any, float], alpha: float, persona_only: bool = False
) -> dict[Any, float]:
    keys = set(p) if persona_only else set(p) | set(g)
    return {key: alpha * p.get(key, 0.0) + (1 - alpha) * g.get(key, 0.0) for key in keys}


def _blend_counts(
    p: StyleAggregate, g: StyleAggregate, attr: str, alpha: float, min_count: int = 1
) -> dict[Any, float]:
    """Blended per-sentence shares of ``p.<attr>`` / ``g.<attr>`` (see module docstring)."""
    pc, gc = getattr(p, attr), getattr(g, attr)
    persona_only = p is not g and p.sample_count >= BLEND_PRIOR
    out = _blend_dicts(_shares(pc, p.sentence_count), _shares(gc, g.sentence_count), alpha, persona_only)
    if min_count > 1:
        out = {key: v for key, v in out.items() if max(pc.get(key, 0), gc.get(key, 0)) >= min_count}
    return out


def _mix(pv: float | None, gv: float | None, alpha: float) -> float:
    if pv is None:
        return gv or 0.0
    if gv is None:
        return pv
    return alpha * pv + (1 - alpha) * gv


def _min_count(p: StyleAggregate) -> int:
    return MIN_LIST_COUNT if p.sample_count >= 5 else 1


def is_lexicon_word(w: str) -> bool:
    return (
        (len(w) >= 2 or w in INFORMAL_SHORT)
        and w not in FUNCTION_WORDS
        and w not in GENERIC_WORDS
        and not w[0].isdigit()
        and not is_contraction(w)
        and not w.endswith("'s")
        and "." not in w
    )


def _lexicon(p: StyleAggregate, g: StyleAggregate, k: int) -> list[str]:
    distinct = g.sample_count > p.sample_count and p.len_sum > 0 and g.len_sum > 0
    min_count = _min_count(p)
    scores: dict[str, float] = {}
    for w, n in p.words.items():
        if n < min_count or not is_lexicon_word(w):
            continue
        ratio = 1.0
        if distinct:
            total = max(p.word_totals.get(w, n), n)
            ratio = (n / p.len_sum) / (total / g.len_sum)
        scores[w] = n * math.log2(1 + ratio)
    return _ranked(scores, k)


def _openers(p: StyleAggregate, g: StyleAggregate, alpha: float, k: int) -> list[str]:
    mc = _min_count(p)
    one = _blend_counts(p, g, "openers", alpha, mc)
    phrases = _blend_counts(p, g, "opener_phrases", alpha, max(mc, 2))
    out: list[str] = []
    for w in _ranked(one, k):
        best = w
        # prefer the longest fixed phrase ("just wanted to") that dominates this opener
        for phrase, s in sorted(phrases.items(), key=lambda kv: (-len(kv[0].split()), -kv[1], kv[0])):
            if phrase.split()[0] == w and s >= OPENER_PHRASE_SHARE * one[w]:
                best = phrase
                break
        if best not in out:
            out.append(best)
    return out


def compute_constraints(agg: StyleAggregate, k: int = 20, global_agg: StyleAggregate | None = None) -> Constraints:
    """Blend ``agg`` (persona) with ``global_agg`` (all personas); see module docstring."""
    g = global_agg if global_agg is not None else agg
    p = agg
    if p.sample_count <= 0 and p.sentence_count <= 0:
        p = g  # persona has no data → pure global
    alpha = blend_weight(p.sample_count, g.sample_count) if p is not g else 1.0
    c = empty_constraints()
    c["sample_count"] = int(p.sample_count)
    if p.sample_count <= 0 and p.sentence_count <= 0:
        return c

    def avg(a: StyleAggregate) -> float | None:
        return a.len_sum / a.sentence_count if a.sentence_count else None

    def p90(a: StyleAggregate) -> float | None:
        return _p90(a.len_hist, a.sentence_count) if a.sentence_count else None

    def lower(a: StyleAggregate) -> float | None:
        return a.lower_starts / a.sentence_count if a.sentence_count else None

    def contr(a: StyleAggregate) -> float | None:
        opp = a.contractions + a.expansions
        return a.contractions / opp if opp else None

    if p.sentence_count or g.sentence_count:
        c["avg_sentence_len"] = round(_mix(avg(p), avg(g), alpha), 2)
        c["sentence_len_p90"] = round(_mix(p90(p), p90(g), alpha), 1)
        lower_share = _mix(lower(p), lower(g), alpha)
        c["casing"] = "lower" if lower_share >= 0.6 else "sentence" if lower_share <= 0.2 else "mixed"
        rates = _blend_counts(p, g, "punctuation", alpha)
        c["punctuation_rates"] = {sym: round(r, 3) for sym, r in sorted(rates.items()) if r > 0}
    c["contractions_rate"] = round(_mix(contr(p), contr(g), alpha), 3)
    c["top_openers"] = _openers(p, g, alpha, min(k, 8))
    trans = _blend_counts(p, g, "transitions", alpha, _min_count(p))
    c["top_transitions"] = _ranked(trans, min(k, 8))
    c["top_bigrams"] = [
        [a, b, float(n)]
        for (a, b), n in sorted(p.bigrams.items(), key=lambda kv: (-kv[1], kv[0]))
        if n >= _min_count(p)
    ][:k]
    structs = _blend_counts(p, g, "structures", alpha)
    c["structures"] = [{"signature": s, "weight": round(structs[s], 4)} for s in _ranked(structs, k)]
    c["lexicon"] = _lexicon(p, g, k)
    return c
