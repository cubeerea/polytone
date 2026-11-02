"""Few-shot example selection: 2–3 short excerpts of the user's REAL writing for the prompt.

Real human sentences are the strongest style signal, so ``build_system_prompt`` shows a few.
They come only from SQLite ``samples`` (text that passed the firewall), never from ``drafts``,
and every chosen excerpt is re-checked against stored drafts before use.

Selection (deterministic):
1. Candidates: single sentences and adjacent sentence pairs (3–40 tokens) from the persona's
   most recent samples (all samples when the persona has fewer than ``MIN_EXAMPLES`` excerpts).
2. Score (lower is better) = |tokens − target| / target          (target ≈ intent length, 6–30)
                            + 0.5 · |avg sentence len − persona avg| / persona avg
                            − 0.5 · share of the excerpt's sentences whose structure signature
                                    is among the persona's top-5 structures
                            − 0.25 · share of its sentences whose signature appears in what the
                                    user KEPT of recent drafts (outcome ``accepted``: the draft;
                                    ``edited``: the message actually sent) — ``OUTCOME_BOOST``
3. Greedy pick by score: at most one excerpt per sample, no two with the same opener.

The outcome boost only re-ranks excerpts of the user's own samples: drafts are never candidates
(only their structure signatures are read), and the draft re-check below still applies.
Its signature set is cached per (database, persona, ``Database.outcome_version``).

Memo: the result is cached per (database, persona, target length, avg, top structures, outcome
signatures, k, samples watermark). Drafts are append-only and only ever *remove* candidates, so a cached
pick stays exactly right as long as each chosen excerpt still passes against the drafts
added since — that is all a cache hit re-checks (a new sample, or a deleted draft, recomputes).
"""
from __future__ import annotations

import logging
import threading
from collections import Counter, OrderedDict
from typing import Any

from backend.ingestion.extract import signature
from backend.ingestion.firewall import draft_match_reason, draft_snapshot
from backend.ingestion.text import clean_message, segment, token_spans, tokenize

log = logging.getLogger(__name__)

MAX_EXAMPLES = 3
MIN_EXAMPLES = 2
MAX_EXCERPT_TOKENS = 40
MIN_EXCERPT_TOKENS = 3
MAX_EXCERPT_CHARS = 280
SAMPLE_LIMIT = 200
OUTCOME_BOOST = 0.25  # bounded: at most 0.25 off the score, half the top-structure term
OUTCOME_DRAFTS = 30  # newest kept drafts read per persona
OUTCOME_SIGS = 12  # most frequent signatures among them that get the boost

# (excerpt text, token count, [signature per sentence], opener)
Excerpt = tuple[str, int, tuple[str, ...], str]

_cache: OrderedDict[tuple[str, int, str], list[Excerpt]] = OrderedDict()
_cache_lock = threading.Lock()
_CACHE_SIZE = 4000

# memo key -> (chosen excerpts, drafts snapshot rows object, drafts checked)
_picks: OrderedDict[tuple, tuple[list[str], Any, int]] = OrderedDict()
_PICKS_SIZE = 256
# (db, persona, outcome_version) -> signatures of kept drafts
_kept: OrderedDict[tuple, tuple[str, ...]] = OrderedDict()
_gen = 0  # bumped by invalidate(): part of every picks key, so a selection racing it is never reused


def invalidate() -> None:
    """Forget every cached pick (samples moved between personas: the watermark doesn't change)."""
    global _gen
    with _cache_lock:
        _gen += 1
        _picks.clear()
        _kept.clear()


def _signatures(text: str) -> list[str]:
    return [signature(seg.text, seg.bullet) for seg in segment(clean_message(text)) if token_spans(seg.text)]


def kept_signatures(db: Any, persona: str | None) -> tuple[str, ...]:
    """The ``OUTCOME_SIGS`` most frequent structure signatures of what the user kept from
    ``persona``'s recent drafts (sorted; deterministic). Cached until an outcome changes."""
    if not persona or not hasattr(db, "kept_draft_texts"):
        return ()
    key = (str(getattr(db, "path", id(db))), persona.lower(), getattr(db, "outcome_version", 0), _gen)
    with _cache_lock:
        hit = _kept.get(key)
    if hit is not None:
        return hit
    counts: Counter[str] = Counter()
    for text in db.kept_draft_texts(persona, OUTCOME_DRAFTS):
        counts.update(_signatures(text))
    top = tuple(sorted(sig for sig, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:OUTCOME_SIGS]))
    with _cache_lock:
        _kept[key] = top
        while len(_kept) > _PICKS_SIZE:
            _kept.popitem(last=False)
    return top


def _excerpts_for(db_key: str, sample: dict[str, Any]) -> list[Excerpt]:
    key = (db_key, int(sample["id"]), sample["sha256"])
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None:
            _cache.move_to_end(key)
            return hit
    sents = []
    for seg in segment(clean_message(sample["text"])):
        toks = token_spans(seg.text)
        if toks:
            sents.append((" ".join(seg.text.split()), len(toks), signature(seg.text, seg.bullet), toks[0].text, seg.bullet))
    out: list[Excerpt] = []
    for i, (text, n, sig, opener, bullet) in enumerate(sents):
        if bullet:
            continue
        if MIN_EXCERPT_TOKENS <= n <= MAX_EXCERPT_TOKENS:
            out.append((text, n, (sig,), opener))
        if i + 1 < len(sents) and not sents[i + 1][4]:
            t2, n2, sig2, _, _ = sents[i + 1]
            if n + n2 <= MAX_EXCERPT_TOKENS:
                out.append((f"{text} {t2}", n + n2, (sig, sig2), opener))
    out = [e for e in out if len(e[0]) <= MAX_EXCERPT_CHARS]
    with _cache_lock:
        _cache[key] = out
        while len(_cache) > _CACHE_SIZE:
            _cache.popitem(last=False)
    return out


def _usable(sample: dict[str, Any]) -> bool:
    prov = sample.get("provenance") or {}
    return not (prov.get("generated") is True or prov.get("origin") == "polytone")


def select_examples(
    db: Any, persona: str | None, intent: str, constraints: dict[str, Any], k: int = MAX_EXAMPLES
) -> list[str]:
    db_key = str(getattr(db, "path", id(db)))
    target = min(max(len(tokenize(intent)), 6), 30)
    avg = float(constraints.get("avg_sentence_len") or 0) or None
    top5 = tuple(s["signature"] for s in (constraints.get("structures") or [])[:5])
    try:
        kept = kept_signatures(db, persona)
    except Exception as e:  # noqa: BLE001 - the outcome boost is optional
        log.warning("kept-draft signatures failed: %s", type(e).__name__)
        kept = ()
    key = (db_key, persona or None, target, avg, top5, kept, k, db.samples_watermark(), _gen)
    drafts = draft_snapshot(db)  # one sync for every draft check below
    with _cache_lock:
        hit = _picks.get(key)
        if hit is not None:
            _picks.move_to_end(key)
    if hit is not None and hit[1] is drafts.rows and hit[2] <= drafts.n:
        chosen, _, checked = hit
        if all(draft_match_reason(t, db, snapshot=drafts, start=checked) is None for t in chosen):
            with _cache_lock:
                _picks[key] = (chosen, drafts.rows, drafts.n)
            return list(chosen)
    chosen = _select(db, db_key, persona, target, avg, set(top5), k, drafts, set(kept))
    with _cache_lock:
        _picks[key] = (chosen, drafts.rows, drafts.n)
        while len(_picks) > _PICKS_SIZE:
            _picks.popitem(last=False)
    return list(chosen)


def _select(
    db: Any, db_key: str, persona: str | None, target: int, avg: float | None, top_sigs: set[str], k: int, drafts: Any,
    kept_sigs: set[str] | None = None,
) -> list[str]:
    samples = [s for s in db.list_samples(persona=persona, limit=SAMPLE_LIMIT) if _usable(s)] if persona else []
    pools = [samples]
    if sum(len(_excerpts_for(db_key, s)) for s in samples) < MIN_EXAMPLES:
        seen = {s["id"] for s in samples}
        pools.append([s for s in db.list_samples(limit=SAMPLE_LIMIT) if _usable(s) and s["id"] not in seen])

    chosen: list[str] = []
    for pool in pools:
        cands: list[tuple[float, int, str, Excerpt]] = []
        for s in pool:
            for ex in _excerpts_for(db_key, s):
                text, n, sigs, _ = ex
                score = abs(n - target) / target
                if avg:
                    score += 0.5 * abs(n / len(sigs) - avg) / avg
                if top_sigs:
                    score -= 0.5 * sum(sig in top_sigs for sig in sigs) / len(sigs)
                if kept_sigs:
                    score -= OUTCOME_BOOST * sum(sig in kept_sigs for sig in sigs) / len(sigs)
                cands.append((round(score, 6), -int(s["id"]), text, ex))
        cands.sort(key=lambda c: (c[0], c[1], c[2]))
        used_samples: set[int] = set()
        used_openers: set[str] = set()
        for _, neg_id, text, ex in cands:
            if len(chosen) >= k:
                break
            if neg_id in used_samples or ex[3] in used_openers or text in chosen:
                continue
            if draft_match_reason(text, db, snapshot=drafts) is not None:
                continue  # never show anything that looks like a generated draft
            chosen.append(text)
            used_samples.add(neg_id)
            used_openers.add(ex[3])
        if len(chosen) >= MIN_EXAMPLES:
            break
    return chosen
