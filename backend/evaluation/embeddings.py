"""Qwen3-Embedding style component (optional).

* Lazy, thread-safe singleton for ``Qwen/Qwen3-Embedding-0.6B`` via sentence-transformers,
  on MPS when available, else CPU.
* Baseline centroid cached per (persona, sample count, content hash) — adding or removing a
  sample changes the key, so the cache invalidates itself.
* Any failure (deps missing, download blocked, OOM) logs once and returns ``None``; the
  scorer then renormalizes over the stylometric components. Never raises.
* ``POLYTONE_EMBEDDINGS=none`` disables everything without importing torch.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Sequence
from typing import Any

log = logging.getLogger(__name__)

MODEL_NAME = "Qwen/Qwen3-Embedding-0.6B"
# Qwen3-Embedding is instruction-aware; steer it toward *how* something is written.
STYLE_PROMPT = (
    "Instruct: Represent the writing style of this text (tone, register, rhythm, punctuation"
    " and word choice), not its topic.\nQuery: "
)
MAX_BASELINE = 64  # cap samples embedded per centroid (most recent first, as given)
_MAX_CACHE = 16

_lock = threading.Lock()
_model: Any = None
_load_failed = False
_load_seconds: float | None = None
_centroids: OrderedDict[tuple, Any] = OrderedDict()


def enabled() -> bool:
    mode = os.environ.get("POLYTONE_EMBEDDINGS")
    if mode is None:
        try:
            from backend.config import get_settings

            mode = get_settings().polytone_embeddings
        except Exception:  # noqa: BLE001
            mode = "qwen"
    return (mode or "").strip().lower() not in ("none", "off", "false", "0", "")


def _device() -> str:
    import torch

    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def get_model() -> Any | None:
    """Load (once) and return the SentenceTransformer, or None if unavailable."""
    global _model, _load_failed, _load_seconds
    if not enabled() or _load_failed:
        return None
    if _model is not None:
        return _model
    with _lock:
        if _model is not None or _load_failed:
            return _model
        try:
            t0 = time.perf_counter()
            from sentence_transformers import SentenceTransformer

            dev = _device()
            _model = SentenceTransformer(MODEL_NAME, device=dev)
            _load_seconds = time.perf_counter() - t0
            log.info("loaded %s on %s in %.1fs", MODEL_NAME, dev, _load_seconds)
        except Exception as e:  # noqa: BLE001 - optional component
            _load_failed = True
            log.warning("embedding model unavailable (%s); scoring without embeddings", e)
            return None
    return _model


def load_seconds() -> float | None:
    return _load_seconds


def embed(texts: Sequence[str]) -> Any | None:
    """L2-normalized embeddings (numpy array, shape [n, d]) or None."""
    model = get_model()
    if model is None or not texts:
        return None
    try:
        with _lock:  # MPS is not happy with concurrent encode calls
            return model.encode(
                list(texts), prompt=STYLE_PROMPT,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
    except Exception as e:  # noqa: BLE001
        log.warning("embedding failed: %s", e)
        return None


def _key(baseline: Sequence[str], persona: str | None) -> tuple:
    h = hashlib.sha256()
    for t in baseline:
        h.update(t.encode("utf-8", "replace"))
        h.update(b"\x00")
    return (persona or "", len(baseline), h.hexdigest())


def baseline_centroid(baseline: Sequence[str], persona: str | None = None) -> Any | None:
    import numpy as np

    texts = [t for t in baseline if t and t.strip()][:MAX_BASELINE]
    if not texts:
        return None
    key = _key(texts, persona)
    if key in _centroids:
        _centroids.move_to_end(key)
        return _centroids[key]
    vecs = embed(texts)
    if vecs is None:
        return None
    c = np.asarray(vecs).mean(axis=0)
    n = float(np.linalg.norm(c))
    if n == 0:
        return None
    c = c / n
    _centroids[key] = c
    while len(_centroids) > _MAX_CACHE:
        _centroids.popitem(last=False)
    return c


def clear_cache() -> None:
    _centroids.clear()


# Cosine similarity below FLOOR maps to distance 1, at 1.0 maps to 0. Qwen embeddings of any
# two English texts rarely fall below ~0.3 cosine, so raw (1 - cos) would compress every
# score into the top of the range; the floor stretches the useful band. See calibrate.py.
COSINE_FLOOR = 0.3


def embedding_distance(text: str, baseline: Sequence[str], persona: str | None = None) -> float | None:
    """Cosine distance (rescaled to [0,1]) between ``text`` and the baseline centroid, or None."""
    if not enabled() or not (text or "").strip():
        return None
    try:
        import numpy as np

        centroid = baseline_centroid(baseline, persona)
        if centroid is None:
            return None
        v = embed([text])
        if v is None:
            return None
        cos = float(np.dot(np.asarray(v)[0], centroid))
        d = (1.0 - cos) / (1.0 - COSINE_FLOOR)
        return max(0.0, min(1.0, d))
    except Exception as e:  # noqa: BLE001
        log.warning("embedding distance failed: %s", e)
        return None
