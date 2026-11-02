"""Recency weighting for the style graph ("growing unit" exponential decay).

A habit from two years ago should count less than one from yesterday, but rewriting every weight
of the graph on every read (or every day) does not scale. Instead every write is multiplied by a
scale that GROWS with time::

    s(t) = 2 ** ((t - t0) / H)          # H = half-life in seconds, t0 = the graph's epoch

and every read divides the aggregates by ``s(now)``. A read therefore sees *effective recent
counts*: an occurrence written now counts 1.0, one written a half-life ago 0.5, two half-lives ago
0.25 — and nothing stored is ever rewritten. Absolute thresholds (``MIN_LIST_COUNT`` …) keep their
meaning because the unit of an effective count is "one occurrence, now".

What decays (multiplied by ``s`` on write, divided by ``s(now)`` on read): ``Word.weight`` /
``persona_counts``, ``FOLLOWED_BY.weight`` / ``persona_weights``, ``PART_OF_STRUCTURE.weight``,
``SentenceStructure.weight``, ``PunctuationMark.weight``, ``USED_IN.weight`` and the IntentContext
counters ``sample_weight, sentence_count, lower_starts, upper_starts, len_sum, len_hist,
contractions, expansions, opener_counts, opener_phrase_counts, transition_counts``.
``IntentContext.sample_count`` stays the raw integer number of samples (the persona / global blend
and the ``sample_count`` constraint need it); ``sample_weight`` is its decayed twin, so
``sample_weight / sample_count`` is how fresh the data is on average (``constraints.py`` scales its
minimum counts by that, so a graph nobody writes to keeps producing the same constraints).

Rebase: ``s`` grows without bound, so once ``s(now) > REBASE_AT`` every decayed value is multiplied
by ``1 / s(now)`` and ``t0`` moves to now (a no-op for every read). With H = 90 days that happens
about every five years. Prune: edges whose effective weight faded below ``PRUNE_BELOW`` (and
words left with no edges and a faded weight) are deleted, so a forgotten habit also stops using
disk. Both run from ``GraphStore.decay_maintenance()`` (daily, ``security/retention.py``).

``H = 0`` disables decay: ``s ≡ 1``, weights stay integers and every result is what it was before
decay existed. Both stores compute the scaled increments here, in Python, and pass them to Neo4j as
parameters, so the in-memory store and Cypher do the same float arithmetic and return identical
aggregates.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

DAY_S = 86400.0
REBASE_AT = 1e6  # rebase once s(now) exceeds this (≈ 20 half-lives)
PRUNE_BELOW = 0.05  # effective weight under which FOLLOWED_BY / PART_OF_STRUCTURE edges are dropped
FRESH_TOLERANCE = 1e-9  # relative slack on freshness-scaled thresholds (float rounding)

# IntentContext properties that decay (scalars, then per-index lists)
CTX_DECAYED_SCALARS = ("sample_weight", "sentence_count", "lower_starts", "upper_starts", "len_sum",
                       "contractions", "expansions")
CTX_DECAYED_LISTS = ("len_hist", "opener_counts", "opener_phrase_counts", "transition_counts")


def scaled(n: Any, s: float) -> Any:
    """``n * s``; ``n`` itself when ``s == 1`` (so a disabled decay keeps integer weights)."""
    return n if s == 1.0 else n * s


def rebase_factor(t0: float, half_life_s: float, now: float) -> float:
    """``1 / s(now)`` for the epoch ``t0`` (1.0 when decay was off). Both stores multiply by this."""
    if not half_life_s or half_life_s <= 0:
        return 1.0
    return 2.0 ** (-(now - t0) / half_life_s)


class Decay:
    """Clock, half-life and epoch of one store. ``t0`` is None until a Neo4j store has read it."""

    def __init__(
        self,
        half_life_days: float = 0.0,
        clock: Callable[[], float] = time.time,
        t0: float | None = None,
        rebase_at: float = REBASE_AT,
    ) -> None:
        self.half_life_s = max(0.0, float(half_life_days or 0.0)) * DAY_S
        self.clock = clock
        self.t0 = t0
        self.rebase_at = float(rebase_at)

    @property
    def enabled(self) -> bool:
        return self.half_life_s > 0

    @property
    def half_life_days(self) -> float:
        return self.half_life_s / DAY_S

    def now(self) -> float:
        return float(self.clock())

    def scale(self, now: float | None = None) -> float:
        """``s(now)``: the multiplier of a write made now (1.0 when disabled)."""
        if not self.enabled or self.t0 is None:
            return 1.0
        t = self.now() if now is None else now
        return 2.0 ** ((t - self.t0) / self.half_life_s)


def scale_context(ctx: dict[str, Any], f: float) -> None:
    """Multiply an IntentContext's decayed properties by ``f`` in place (in-memory rebase; the
    Cypher ``REBASE_CONTEXTS`` does the same arithmetic)."""
    if ctx.get("sample_weight") is None:
        ctx["sample_weight"] = ctx.get("sample_count") or 0
    for k in CTX_DECAYED_SCALARS:
        ctx[k] = (ctx.get(k) or 0) * f
    for k in CTX_DECAYED_LISTS:
        ctx[k] = [x * f for x in ctx.get(k) or []]


class RebaseLock:
    """Shared / exclusive lock: graph reads and writes take it shared, a rebase exclusive (a write
    in old units landing on an already-rebased node would be off by the rebase factor). Waiting
    exclusive holders block new shared ones. Not reentrant — never nest."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._shared = 0
        self._exclusive = False
        self._waiting = 0

    @contextmanager
    def shared(self) -> Iterator[None]:
        with self._cond:
            while self._exclusive or self._waiting:
                self._cond.wait()
            self._shared += 1
        try:
            yield
        finally:
            with self._cond:
                self._shared -= 1
                self._cond.notify_all()

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        with self._cond:
            self._waiting += 1
            try:
                while self._exclusive or self._shared:
                    self._cond.wait()
            finally:
                self._waiting -= 1
            self._exclusive = True
        try:
            yield
        finally:
            with self._cond:
                self._exclusive = False
                self._cond.notify_all()
