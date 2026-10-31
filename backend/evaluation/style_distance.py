"""Style Distance Score — how close a text is to the user's own human writing.

``score(text, baseline_texts, persona=None) -> ScoreBreakdown`` (see CONTRACTS.md):

    {"score": 0-100, "distance": 0-1,
     "components": {"sentence_length", "punctuation", "lexical", "function_words", "embedding"|None},
     "confidence": "low"|"medium"|"high"}

Weights (sum to 1; renormalized over the available components when ``embedding`` is None):

* function_words 0.25 — Burrows' Delta is the best-validated authorship signal and is mostly
  topic-independent, so it carries the most weight.
* embedding      0.25 — Qwen3-Embedding with a style instruction captures register/tone that
  counts can't, but it is still partly topical (a draft on a new subject drifts even when
  the voice is right), so it is not allowed to dominate.
* punctuation    0.20 — very personal (dash/ellipsis/exclamation habits) and cheap to match.
* lexical        0.15 — casing, contractions, word length, MATTR, char 3-grams.
* sentence_length 0.15 — rhythm; useful but noisy for short drafts with 1-3 sentences.

Feature-only (embedding None) weights therefore become 0.333 / 0.267 / 0.2 / 0.2.

Confidence reflects how trustworthy the number is, not how good the draft is: it is
``low`` with < 3 baseline samples, < 150 baseline words or a draft < 8 words; ``high``
with ≥ 10 samples, ≥ 600 baseline words and a draft ≥ 25 words; ``medium`` otherwise.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from backend.evaluation import embeddings
from backend.evaluation.features import (
    Baseline,
    build_baseline,
    extract_features,
    function_word_distance,
    lexical_distance,
    punctuation_distance,
    sentence_length_distance,
)

log = logging.getLogger(__name__)

WEIGHTS: dict[str, float] = {
    "sentence_length": 0.15,
    "punctuation": 0.20,
    "lexical": 0.15,
    "function_words": 0.25,
    "embedding": 0.25,
}


def _confidence(base: Baseline, text_words: int) -> str:
    n, bw = base.n_samples, base.pooled.n_words
    if n < 3 or bw < 150 or text_words < 8:
        return "low"
    if n >= 10 and bw >= 600 and text_words >= 25:
        return "high"
    return "medium"


def combine(components: dict[str, float | None]) -> float:
    """Weighted mean over the non-None components (weights renormalized)."""
    avail = {k: v for k, v in components.items() if v is not None}
    tot = sum(WEIGHTS[k] for k in avail)
    if tot == 0:
        return 1.0
    return sum(WEIGHTS[k] * v for k, v in avail.items()) / tot


def _breakdown(distance: float, components: dict[str, float | None], confidence: str) -> dict[str, Any]:
    distance = max(0.0, min(1.0, distance))
    return {
        "score": round(100.0 * (1.0 - distance), 1),
        "distance": round(distance, 4),
        "components": {k: (None if v is None else round(max(0.0, min(1.0, v)), 4)) for k, v in components.items()},
        "confidence": confidence,
    }


def score(
    text: str,
    baseline_texts: Sequence[str],
    persona: str | None = None,
    *,
    use_embeddings: bool | None = None,
) -> dict[str, Any]:
    """Score ``text`` against ``baseline_texts`` (the persona's human samples).

    Never raises for bad input: empty text or an empty baseline yields distance 1.0 with
    ``confidence: "low"``. ``use_embeddings=False`` forces feature-only mode (deterministic).
    """
    baseline = [t for t in (baseline_texts or []) if t and t.strip()]
    tf = extract_features(text or "")
    base = build_baseline(baseline)
    if tf.n_words == 0 or base.pooled.n_words == 0:
        comps: dict[str, float | None] = {
            "sentence_length": 1.0,
            "punctuation": 1.0,
            "lexical": 1.0,
            "function_words": 1.0,
            "embedding": None,
        }
        return _breakdown(1.0, comps, "low")

    comps = {
        "sentence_length": sentence_length_distance(tf, base),
        "punctuation": punctuation_distance(tf, base),
        "lexical": lexical_distance(tf, base),
        "function_words": function_word_distance(tf, base),
        "embedding": None,
    }
    if use_embeddings is None:
        use_embeddings = embeddings.enabled()
    if use_embeddings:
        comps["embedding"] = embeddings.embedding_distance(text, baseline, persona)
    return _breakdown(combine(comps), comps, _confidence(base, tf.n_words))


def score_draft(draft_id: str, runtime_or_db: Any, *, limit: int = 200) -> dict[str, Any] | None:
    """(Re)score a stored draft against its persona's samples and persist the breakdown.

    Accepts a ``Runtime`` (uses ``.db``) or a ``Database``. Mirrors the baseline selection of
    ``generate_graph.score_draft`` (persona samples, falling back to all samples). Useful for
    backfills; the live generate graph calls ``score(text, baseline)`` directly.
    """
    db = getattr(runtime_or_db, "db", runtime_or_db)
    draft = db.get_draft(draft_id)
    if not draft:
        return None
    persona = draft.get("persona") or None
    baseline = [s["text"] for s in db.list_samples(persona=persona, limit=limit)]
    if not baseline:
        baseline = [s["text"] for s in db.list_samples(limit=limit)]
    if not baseline:
        return None
    try:
        result = score(draft["text"], baseline, persona)
    except Exception as e:  # noqa: BLE001 - scoring is best-effort
        log.warning("scoring draft %s failed: %s", draft_id, e)
        return None
    db.update_draft_score(draft_id, result)
    return result
