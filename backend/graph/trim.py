"""Bound what one sample can write to the style graph, so large or repetitive text scales.

The graph stores counts, never occurrences: a word written 10,000 times in one sample is one
``Word`` row with ``n = 10000``, and the same holds for bigrams, structures and openers. What
still grows with text size is the number of *distinct* rows, and common words (``the``, ``and``,
``of``) are the worst case: they precede and follow almost every other word, so their
``FOLLOWED_BY`` fan-out grows with the whole vocabulary and they become supernodes that every
bigram read and MERGE has to walk.

Two layers keep that bounded:

1. ``trim_extraction`` (per sample, before any store sees it) keeps only the highest-count rows
   of each kind, and at most ``SAMPLE_FANOUT`` distinct followers / predecessors per word.
   Counts of the rows it keeps are exact. Sentence statistics (lengths, casing, contractions,
   expansions) come from the untrimmed extraction, so they stay exact too.
2. ``GRAPH_FANOUT`` (graph-wide, applied by both stores after every write): a word keeps at most
   that many outgoing and incoming ``FOLLOWED_BY`` edges; the lowest-weight ones are dropped
   (ties: blind index ascending, identical in both stores). Style reads only ever use the top
   bigrams, so the dropped long tail never reached a constraint anyway.

Writes are also sent in chunks of ``WRITE_CHUNK`` rows (one transaction, several UNWINDs) so a
single huge sample does not become one giant Cypher parameter.

``prepare(ex, scale)`` also applies recency weighting (``graph/decay.py``): every count it returns
is multiplied by the store's current scale ``s(now)``, once, here, so both stores write the same
floats. Trimming and fan-out ranking happen on the raw per-sample counts, before scaling.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from typing import Any, TypeVar

from backend.graph.constraints import context_delta
from backend.graph.decay import scaled
from backend.ingestion.types import Extraction

MAX_WORDS = 3000  # distinct Word rows per sample
MAX_BIGRAMS = 6000  # distinct FOLLOWED_BY rows per sample
MAX_WORD_STRUCTURES = 2000  # distinct PART_OF_STRUCTURE rows per sample
MAX_STRUCTURES = 500  # distinct SentenceStructure rows per sample
MAX_KEYED = 200  # distinct openers / opener phrases / transitions per sample
SAMPLE_FANOUT = 64  # distinct followers (and predecessors) per word, per sample
GRAPH_FANOUT = 256  # FOLLOWED_BY edges per word and direction, graph-wide
WRITE_CHUNK = 1000  # rows per UNWIND

K = TypeVar("K")


def top_counts(d: dict[K, int], n: int) -> dict[K, int]:
    """The ``n`` highest counts (ties: key ascending, so the result is deterministic)."""
    if len(d) <= n:
        return dict(d)
    return dict(sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))[:n])


def cap_fanout(bigrams: dict[tuple[str, str], int], cap: int) -> dict[tuple[str, str], int]:
    """Keep at most ``cap`` followers per first word, then at most ``cap`` predecessors per
    second word (highest counts first, ties on the other word)."""
    out: dict[tuple[str, str], int] = {}
    by_a: dict[str, list[tuple[tuple[str, str], int]]] = defaultdict(list)
    for bg, n in bigrams.items():
        by_a[bg[0]].append((bg, n))
    for rows in by_a.values():
        rows.sort(key=lambda r: (-r[1], r[0][1]))
        out.update(rows[:cap])
    by_b: dict[str, list[tuple[tuple[str, str], int]]] = defaultdict(list)
    for bg, n in out.items():
        by_b[bg[1]].append((bg, n))
    kept: dict[tuple[str, str], int] = {}
    for rows in by_b.values():
        rows.sort(key=lambda r: (-r[1], r[0][0]))
        kept.update(rows[:cap])
    return {bg: n for bg, n in bigrams.items() if bg in kept}  # original order


def trim_extraction(ex: Extraction) -> Extraction:
    """A copy of ``ex`` with every graph-bound mapping bounded (see module docstring)."""
    words = top_counts(ex.words, MAX_WORDS)
    bigrams = {bg: n for bg, n in ex.bigrams.items() if bg[0] in words and bg[1] in words}
    bigrams = top_counts(cap_fanout(bigrams, SAMPLE_FANOUT), MAX_BIGRAMS)
    structures = top_counts(ex.structures, MAX_STRUCTURES)
    wstructs = {k: n for k, n in ex.word_structures.items() if k[0] in words and k[1] in structures}
    return replace(
        ex,
        words=words,
        bigrams=bigrams,
        structures=structures,
        word_structures=top_counts(wstructs, MAX_WORD_STRUCTURES),
        openers=top_counts(ex.openers, MAX_KEYED),
        opener_ngrams=top_counts(ex.opener_ngrams, MAX_KEYED),
        transitions=top_counts(ex.transitions, MAX_KEYED),
    )


def prepare(ex: Extraction, scale: float = 1.0) -> tuple[Extraction, dict[str, Any]]:
    """(trimmed extraction, IntentContext delta), every graph-bound count multiplied by ``scale``
    (the decay multiplier ``s(now)``; 1.0 leaves the integer counts untouched). The delta's
    sentence statistics come from the full sample; its opener / phrase / transition counts from
    the trimmed one. ``sample_weight`` is the sample's decayed weight (``scale``); the raw
    ``sample_count`` increment is always 1 and is not part of the delta."""
    trimmed = trim_extraction(ex)
    d = context_delta(ex)
    d.update(openers=dict(trimmed.openers), opener_phrases=dict(trimmed.opener_ngrams),
             transitions=dict(trimmed.transitions))
    d["sample_weight"] = scaled(1, scale)
    if scale == 1.0:
        return trimmed, d

    def m(x: dict[K, int]) -> dict[K, Any]:
        return {k: n * scale for k, n in x.items()}

    for f in ("sentence_count", "lower_starts", "upper_starts", "len_sum", "contractions", "expansions"):
        d[f] = d[f] * scale
    d["len_hist"] = [n * scale for n in d["len_hist"]]
    for f in ("openers", "opener_phrases", "transitions"):
        d[f] = m(d[f])
    trimmed = replace(
        trimmed,
        words=m(trimmed.words),
        bigrams=m(trimmed.bigrams),
        structures=m(trimmed.structures),
        word_structures=m(trimmed.word_structures),
        punctuation=m(trimmed.punctuation),
    )
    return trimmed, d


def chunks(rows: list[Any], size: int = WRITE_CHUNK) -> list[list[Any]]:
    return [rows[i : i + size] for i in range(0, len(rows), size)]
