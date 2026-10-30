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
3. Greedy pick by score: at most one excerpt per sample, no two with the same opener.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any

from backend.ingestion.extract import signature
from backend.ingestion.firewall import draft_match_reason
from backend.ingestion.text import clean_message, segment, token_spans, tokenize

MAX_EXAMPLES = 3
MIN_EXAMPLES = 2
MAX_EXCERPT_TOKENS = 40
MIN_EXCERPT_TOKENS = 3
MAX_EXCERPT_CHARS = 280
SAMPLE_LIMIT = 200

# (excerpt text, token count, [signature per sentence], opener)
Excerpt = tuple[str, int, tuple[str, ...], str]

_cache: OrderedDict[tuple[str, int, str], list[Excerpt]] = OrderedDict()
_cache_lock = threading.Lock()
_CACHE_SIZE = 4000


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
    samples = [s for s in db.list_samples(persona=persona, limit=SAMPLE_LIMIT) if _usable(s)] if persona else []
    pools = [samples]
    if sum(len(_excerpts_for(db_key, s)) for s in samples) < MIN_EXAMPLES:
        seen = {s["id"] for s in samples}
        pools.append([s for s in db.list_samples(limit=SAMPLE_LIMIT) if _usable(s) and s["id"] not in seen])

    target = min(max(len(tokenize(intent)), 6), 30)
    avg = float(constraints.get("avg_sentence_len") or 0) or None
    top_sigs = {s["signature"] for s in (constraints.get("structures") or [])[:5]}

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
                cands.append((round(score, 6), -int(s["id"]), text, ex))
        cands.sort(key=lambda c: (c[0], c[1], c[2]))
        used_samples: set[int] = set()
        used_openers: set[str] = set()
        for _, neg_id, text, ex in cands:
            if len(chosen) >= k:
                break
            if neg_id in used_samples or ex[3] in used_openers or text in chosen:
                continue
            if draft_match_reason(text, db) is not None:
                continue  # never show anything that looks like a generated draft
            chosen.append(text)
            used_samples.add(neg_id)
            used_openers.add(ex[3])
        if len(chosen) >= MIN_EXAMPLES:
            break
    return chosen
