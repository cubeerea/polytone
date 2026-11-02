"""Edit learning: what the user changes in a draft before sending it corrects the style rules.

The style graph learns what the user writes. This module learns what the user *fixes*. After an
inject, typing capture reports the message the user actually sent (``ingest_typed`` with
``after_inject``). When that message is an edit of a recent draft, the difference between the two
says where the constraints were wrong: sentences too long, dashes the user always deletes, words
they never use.

Pipeline
--------
1. ``match_draft``: among the drafts of the last ``DRAFT_EDIT_WINDOW_S``, the one whose 3-word
   shingles are most contained in the sent text. Below ``MIN_MATCH`` the message is treated as
   something else the user wrote; it is stored unmatched and nothing is learned.
2. ``edit_features``: deltas sent − draft from ``extract()`` of both (average sentence length,
   contraction rate, lowercase-start share, per-sentence rate of each punctuation mark) and, for
   every distinct word of the draft, whether the user deleted it.
3. ``learn_edit``: each feature updates a per-persona row in the ``corrections`` table with a
   smoothed mean (``Database.update_corrections``): the first edits count fully, later ones with
   weight ``ALPHA``, so recent edits matter most and one odd edit cannot swing a rule.
4. ``apply_corrections``: generation adjusts the persona's graph constraints before building the
   prompt. Every adjustment is bounded (``MAX_SHIFT``) and needs ``MIN_EDITS`` edits behind it.

Firewall: only these numeric deltas are learned. The sent text is (partly) model-written, so it
is never ingested as a sample and never reaches the graph (``firewall.py`` still rejects it).
"""
from __future__ import annotations

import threading
from typing import Any

from backend.ingestion.extract import extract
from backend.ingestion.text import containment, shingles, tokenize

MIN_MATCH = 0.3  # share of the draft's shingles the sent text must contain
MIN_EDITS = 3  # edits behind a correction before it changes the prompt
MAX_SHIFT = 0.3  # a scalar moves at most ±30% of its graph value (contraction rate: ±0.3)
AVOID_RATE = 0.6  # a word the user deletes from ≥60% of the drafts that contain it is avoided
MAX_AVOID_WORDS = 12
MATCH_WINDOW_S = 600  # same window typing capture uses to flag a send as after_inject
PUNCT = ("—", "!", "!!", "…", ";", "?", ",", "(", ":", "emoji")

# feature names in the corrections table
EDITS, LEN, CONTRACTIONS, LOWER, PUNCT_F, WORD = "edits", "len", "contractions", "lower", "punct", "word"


def match_draft(db: Any, text: str, within_s: float = MATCH_WINDOW_S) -> tuple[dict[str, Any], float] | None:
    """(draft, containment) of the recent draft this message is an edit of, or None."""
    sent = shingles(text)
    if not sent:
        return None
    best: tuple[dict[str, Any], float] | None = None
    for d in db.drafts_within(within_s):
        score = containment(shingles(d["text"]), sent)
        if score >= MIN_MATCH and (best is None or score > best[1]):
            best = (d, score)
    return best


def _stats(text: str) -> dict[str, Any]:
    ex = extract(text)
    n = ex.sentence_count
    out: dict[str, Any] = {"n": n}
    if n:
        out["len"] = sum(ex.sentence_lengths) / n
        out["punct"] = {s: ex.punctuation.get(s, 0) / n for s in PUNCT}
    cased = ex.lower_starts + ex.upper_starts
    if cased:
        out["lower"] = ex.lower_starts / cased
    if ex.contractions + ex.expansions:
        out["contractions"] = ex.contractions / (ex.contractions + ex.expansions)
    return out


def _words(text: str) -> set[str]:
    return {t for t in tokenize(text) if any(ch.isalpha() for ch in t)}


def edit_features(draft: str, sent: str) -> dict[str, Any]:
    """``{"scalars": {feature: delta}, "punct": {mark: delta}, "words": {word: deleted 0/1}}``.

    Deltas are sent − draft. A scalar is left out when either side has nothing to measure (no
    sentence, no cased start, no contraction or expandable form)."""
    a, b = _stats(draft), _stats(sent)
    scalars = {f: b[f] - a[f] for f in (LEN, CONTRACTIONS, LOWER) if f in a and f in b}
    punct = {s: b["punct"][s] - a["punct"][s] for s in PUNCT} if a["n"] and b["n"] else {}
    kept = _words(sent)  # a word the user moved or kept somewhere is not deleted
    words = {w: 0.0 if w in kept else 1.0 for w in _words(draft)}
    return {"scalars": scalars, "punct": punct, "words": words}


def learn_edit(db: Any, persona: str, draft: str, sent: str) -> dict[str, Any]:
    """Fold one (draft, sent) pair into the persona's corrections. Returns the features."""
    f = edit_features(draft, sent)
    rows: list[tuple[str, str, str | None, float]] = [(EDITS, "", None, 1.0)]
    rows += [(k, "", None, v) for k, v in f["scalars"].items()]
    rows += [(PUNCT_F, s, None, v) for s, v in f["punct"].items()]
    rows += [(WORD, w, w, v) for w, v in sorted(f["words"].items())]
    db.update_corrections(persona, rows)
    return f


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def apply_corrections(constraints: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    """A copy of ``constraints`` adjusted by the persona's ``corrections`` rows (see module
    docstring). Adds ``avoid_words`` / ``avoid_punctuation`` / ``edits_learned``. With no
    corrections the result equals the input."""
    if not rows:
        return constraints
    c = dict(constraints)
    by = {(r["feature"], r["key"]): r for r in rows}
    edits = int(by.get((EDITS, ""), {}).get("n") or 0)
    c["edits_learned"] = edits
    if not int(c.get("sample_count") or 0):
        return c  # nothing learned yet to correct; corrections apply on top of a real style

    def ready(feature: str, key: str = "") -> float | None:
        r = by.get((feature, key))
        return float(r["value"]) if r and int(r["n"]) >= MIN_EDITS else None

    if (d := ready(LEN)) is not None and float(c.get("avg_sentence_len") or 0) > 0:
        avg = float(c["avg_sentence_len"])
        shift = _clamp(d, -MAX_SHIFT * avg, MAX_SHIFT * avg)
        c["avg_sentence_len"] = max(1.0, avg + shift)  # unrounded: rounding could cross the cap
        if float(c.get("sentence_len_p90") or 0):
            c["sentence_len_p90"] = max(c["avg_sentence_len"], float(c["sentence_len_p90"]) + shift)
    if (d := ready(CONTRACTIONS)) is not None:
        rate = float(c.get("contractions_rate") or 0)
        c["contractions_rate"] = round(_clamp(rate + _clamp(d, -MAX_SHIFT, MAX_SHIFT), 0.0, 1.0), 3)
    if (d := ready(LOWER)) is not None:
        if d >= 0.5:
            c["casing"] = "lower"
        elif d <= -0.5:
            c["casing"] = "sentence"
    rates = dict(c.get("punctuation_rates") or {})
    avoid_punct: list[str] = []
    for sym in PUNCT:
        d = ready(PUNCT_F, sym)
        if d is None:
            continue
        rate = float(rates.get(sym, 0.0))
        if rate > 0 and d <= -0.5 * rate:  # the user removes most of these marks
            rates[sym] = 0.0
            avoid_punct.append(sym)
        elif rate > 0:
            rates[sym] = round(_clamp(rate + d, rate * (1 - MAX_SHIFT), rate * (1 + MAX_SHIFT)), 3)
        elif d > 0:  # the user adds a mark the rules never asked for
            rates[sym] = round(min(d, 0.5), 3)
    c["punctuation_rates"] = rates
    c["avoid_punctuation"] = avoid_punct
    words = avoid_words(rows)
    c["avoid_words"] = words
    if words:
        bad = set(words)
        c["lexicon"] = [w for w in c.get("lexicon") or [] if w not in bad]
        c["top_openers"] = [o for o in c.get("top_openers") or [] if o.split()[0] not in bad]
        c["top_transitions"] = [t for t in c.get("top_transitions") or [] if not bad & set(t.split())]
        c["top_bigrams"] = [bg for bg in c.get("top_bigrams") or [] if bg[0] not in bad and bg[1] not in bad]
    return c


def avoid_words(rows: list[dict[str, Any]]) -> list[str]:
    """Words the user deletes from at least AVOID_RATE of the drafts containing them."""
    avoid = sorted(
        (r for r in rows if r["feature"] == WORD and int(r["n"]) >= MIN_EDITS and float(r["value"]) >= AVOID_RATE
         and r.get("text")),
        key=lambda r: (-float(r["value"]) * int(r["n"]), r["text"]),
    )
    return [r["text"] for r in avoid[:MAX_AVOID_WORDS]]


def summary(db: Any, persona: str) -> dict[str, Any]:
    """What edit learning currently changes for ``persona`` (``GET /learning``)."""
    rows = db.get_corrections(persona)
    by = {(r["feature"], r["key"]): r for r in rows}

    def val(feature: str, key: str = "") -> dict[str, Any] | None:
        r = by.get((feature, key))
        return {"value": round(float(r["value"]), 3), "edits": int(r["n"])} if r else None

    return {
        "persona": persona,
        "edits_learned": int(by.get((EDITS, ""), {}).get("n") or 0),
        "min_edits": MIN_EDITS,
        "sentence_len_delta": val(LEN),
        "contractions_delta": val(CONTRACTIONS),
        "lowercase_delta": val(LOWER),
        "punctuation": {s: v for s in PUNCT if (v := val(PUNCT_F, s))},
        "avoid_words": avoid_words(rows),
    }


# --------------------------------------------------------------------------- generation cache
# Corrections change only when an edit is learned, so generation reads them once per persona.
_CACHE: dict[str, list[dict[str, Any]]] = {}
_CACHE_LOCK = threading.Lock()


def invalidate(persona: str | None = None) -> None:
    with _CACHE_LOCK:
        if persona is None:
            _CACHE.clear()
            return
        for k in [k for k in _CACHE if k.endswith("\x00" + persona.lower())]:
            del _CACHE[k]


def corrected(db: Any, persona: str, constraints: dict[str, Any]) -> dict[str, Any]:
    """``constraints`` with ``persona``'s edit corrections applied (cached per persona)."""
    key = f"{getattr(db, 'path', id(db))}\x00{persona.lower()}"
    with _CACHE_LOCK:
        rows = _CACHE.get(key)
    if rows is None:
        rows = db.get_corrections(persona)
        with _CACHE_LOCK:
            _CACHE[key] = rows
    return apply_corrections(constraints, rows)
