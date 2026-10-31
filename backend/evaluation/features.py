"""Deterministic stylometric features and per-component distances.

Pure python + stdlib, no network, no randomness: the same inputs always give the same
numbers. Every ``*_distance`` function returns a float in [0, 1] (0 = same style).

Feature families
----------------
* sentence length  — mean, std, histogram over word-count buckets
* punctuation      — rate per 100 words for , . ! ? ; : — … ( ) " and emoji
* function words   — relative frequency of 150 English function words, compared with a
                     Burrows' Delta (mean |z|) against the baseline's per-sample spread
* lexical          — length-robust type-token ratio (MATTR), avg word length, contraction
                     rate, casing pattern, plus a character 3-gram profile (cosine)
"""
from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- tokenizing

_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z]+)*")
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+|\n+")
_CONTRACTION_RE = re.compile(r"^[A-Za-z]+['’](?:s|t|re|ve|ll|d|m)$", re.IGNORECASE)
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF\U0001F900-\U0001F9FF]"
)

PUNCTUATION_MARKS: dict[str, re.Pattern[str]] = {
    ",": re.compile(r","),
    ".": re.compile(r"(?<!\.)\.(?!\.)"),  # single period, not part of "..."
    "!": re.compile(r"!"),
    "?": re.compile(r"\?"),
    ";": re.compile(r";"),
    ":": re.compile(r":(?!//)"),
    "—": re.compile(r"—|–|(?<=\s)--?(?=\s)"),  # em/en dash, spaced hyphen(s)
    "…": re.compile(r"…|\.{3,}"),
    "(": re.compile(r"\("),
    ")": re.compile(r"\)"),
    '"': re.compile(r"[\"“”]"),
    "emoji": _EMOJI_RE,
}

# Bucket edges (words per sentence): [1-3], [4-7], [8-12], [13-18], [19-25], [26-35], [36+]
SENTENCE_BUCKETS: tuple[int, ...] = (3, 7, 12, 18, 25, 35)

_FUNCTION_WORDS_TEXT = """
    a about after against all also although am among an and another any are around as at
    be because been before being between both but by can could did do does doing down
    during each either enough even ever every few for from further had has have having he
    her here him his how however i if in into is it its just least less like may me might
    more most much must my neither no nor not now of off often on once one only or other
    our out over own perhaps quite rather same shall she should since so some such than
    that the their them then there therefore these they this those though through thus to
    too toward under until up upon us very was we well were what whatever when where
    whether which while who whom whose why will with within without would yet you your
"""
FUNCTION_WORDS: tuple[str, ...] = tuple(_FUNCTION_WORDS_TEXT.split())
_FW_SET = frozenset(FUNCTION_WORDS)


def words(text: str) -> list[str]:
    return _WORD_RE.findall(text or "")


def sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENT_SPLIT_RE.split((text or "").strip())]
    return [p for p in parts if words(p)]


# --------------------------------------------------------------------------- feature dataclass


@dataclass
class TextFeatures:
    """Raw counts for one text (or a pooled set of texts). Pooling = summing counts."""

    n_words: int = 0
    n_chars_in_words: int = 0
    sentence_lengths: list[int] = field(default_factory=list)
    punct_counts: Counter = field(default_factory=Counter)
    fw_counts: Counter = field(default_factory=Counter)
    n_contractions: int = 0
    # casing: sentences starting lowercase / uppercase; standalone "i" vs "I"; ALLCAPS words
    n_sent_lower_start: int = 0
    n_sent_upper_start: int = 0
    n_lower_i: int = 0
    n_upper_i: int = 0
    n_allcaps: int = 0
    trigrams: Counter = field(default_factory=Counter)
    mattr: float = 0.0  # length-robust type-token ratio (weighted when pooled)

    def merge(self, other: TextFeatures) -> TextFeatures:
        tot = self.n_words + other.n_words
        out = TextFeatures(
            n_words=tot,
            n_chars_in_words=self.n_chars_in_words + other.n_chars_in_words,
            sentence_lengths=self.sentence_lengths + other.sentence_lengths,
            punct_counts=self.punct_counts + other.punct_counts,
            fw_counts=self.fw_counts + other.fw_counts,
            n_contractions=self.n_contractions + other.n_contractions,
            n_sent_lower_start=self.n_sent_lower_start + other.n_sent_lower_start,
            n_sent_upper_start=self.n_sent_upper_start + other.n_sent_upper_start,
            n_lower_i=self.n_lower_i + other.n_lower_i,
            n_upper_i=self.n_upper_i + other.n_upper_i,
            n_allcaps=self.n_allcaps + other.n_allcaps,
            trigrams=self.trigrams + other.trigrams,
            mattr=(self.mattr * self.n_words + other.mattr * other.n_words) / tot if tot else 0.0,
        )
        return out

    # ---- derived stats
    @property
    def mean_sentence_len(self) -> float:
        s = self.sentence_lengths
        return sum(s) / len(s) if s else 0.0

    @property
    def std_sentence_len(self) -> float:
        s = self.sentence_lengths
        if len(s) < 2:
            return 0.0
        m = sum(s) / len(s)
        return math.sqrt(sum((x - m) ** 2 for x in s) / (len(s) - 1))

    def sentence_histogram(self) -> list[float]:
        counts = [0] * (len(SENTENCE_BUCKETS) + 1)
        for n in self.sentence_lengths:
            idx = next((i for i, edge in enumerate(SENTENCE_BUCKETS) if n <= edge), len(SENTENCE_BUCKETS))
            counts[idx] += 1
        tot = sum(counts)
        return [c / tot for c in counts] if tot else [0.0] * len(counts)

    def punctuation_rates(self) -> dict[str, float]:
        """Occurrences per 100 words."""
        denom = max(self.n_words, 1)
        return {k: 100.0 * self.punct_counts.get(k, 0) / denom for k in PUNCTUATION_MARKS}

    def function_word_freqs(self) -> dict[str, float]:
        denom = max(self.n_words, 1)
        return {w: self.fw_counts.get(w, 0) / denom for w in FUNCTION_WORDS}

    @property
    def avg_word_len(self) -> float:
        return self.n_chars_in_words / self.n_words if self.n_words else 0.0

    @property
    def contraction_rate(self) -> float:
        return self.n_contractions / self.n_words if self.n_words else 0.0

    @property
    def lowercase_start_rate(self) -> float:
        tot = self.n_sent_lower_start + self.n_sent_upper_start
        return self.n_sent_lower_start / tot if tot else 0.5

    @property
    def lowercase_i_rate(self) -> float | None:
        tot = self.n_lower_i + self.n_upper_i
        return self.n_lower_i / tot if tot else None

    @property
    def allcaps_rate(self) -> float:
        return self.n_allcaps / self.n_words if self.n_words else 0.0

    def casing(self) -> str:
        r = self.lowercase_start_rate
        if r >= 0.8:
            return "lower"
        if r <= 0.2:
            return "sentence"
        return "mixed"

    def as_dict(self) -> dict:
        """Human-readable summary (for debugging / calibration reports)."""
        return {
            "n_words": self.n_words,
            "n_sentences": len(self.sentence_lengths),
            "mean_sentence_len": round(self.mean_sentence_len, 2),
            "std_sentence_len": round(self.std_sentence_len, 2),
            "sentence_hist": [round(x, 3) for x in self.sentence_histogram()],
            "punctuation_per_100w": {k: round(v, 2) for k, v in self.punctuation_rates().items() if v},
            "mattr": round(self.mattr, 3),
            "avg_word_len": round(self.avg_word_len, 2),
            "contraction_rate": round(self.contraction_rate, 3),
            "casing": self.casing(),
        }


def _mattr(tokens: Sequence[str], window: int = 50) -> float:
    """Moving-average type-token ratio (Covington & McFall) — stable across text lengths."""
    n = len(tokens)
    if n == 0:
        return 0.0
    if n <= window:
        # short texts: plain TTR, which is what MATTR degrades to anyway
        return len(set(tokens)) / n
    counts: Counter = Counter(tokens[:window])
    total = len(counts)
    for i in range(window, n):
        out_tok, in_tok = tokens[i - window], tokens[i]
        counts[out_tok] -= 1
        if counts[out_tok] == 0:
            del counts[out_tok]
        counts[in_tok] += 1
        total += len(counts)
    return total / (n - window + 1)


def _char_trigrams(text: str) -> Counter:
    t = re.sub(r"\s+", " ", (text or "").strip())
    return Counter(t[i : i + 3] for i in range(len(t) - 2))


def extract_features(text: str) -> TextFeatures:
    text = text or ""
    toks = words(text)
    lower = [t.lower().replace("’", "'") for t in toks]
    f = TextFeatures(n_words=len(toks), n_chars_in_words=sum(len(t) for t in toks))
    sents = sentences(text)
    f.sentence_lengths = [len(words(s)) for s in sents]
    for s in sents:
        first = next((ch for ch in s if ch.isalpha()), "")
        if first.islower():
            f.n_sent_lower_start += 1
        elif first.isupper():
            f.n_sent_upper_start += 1
    for mark, rx in PUNCTUATION_MARKS.items():
        c = len(rx.findall(text))
        if c:
            f.punct_counts[mark] = c
    f.fw_counts = Counter(w for w in lower if w in _FW_SET)
    f.n_contractions = sum(1 for t in toks if _CONTRACTION_RE.match(t.replace("’", "'")))
    for t in toks:
        if t == "i":
            f.n_lower_i += 1
        elif t == "I":
            f.n_upper_i += 1
        elif len(t) >= 2 and t.isalpha() and t.isupper():
            f.n_allcaps += 1
    f.trigrams = _char_trigrams(text.lower())
    f.mattr = _mattr(lower)
    return f


# --------------------------------------------------------------------------- baseline


@dataclass
class Baseline:
    pooled: TextFeatures
    per_sample: list[TextFeatures]
    fw_mean: dict[str, float]
    fw_std: dict[str, float]

    @property
    def n_samples(self) -> int:
        return len(self.per_sample)


def build_baseline(texts: Iterable[str]) -> Baseline:
    per = [extract_features(t) for t in texts if t and t.strip()]
    pooled = TextFeatures()
    for f in per:
        pooled = pooled.merge(f)
    fw_mean = pooled.function_word_freqs()
    fw_std: dict[str, float] = {}
    voiced = [f for f in per if f.n_words > 0]
    for w in FUNCTION_WORDS:
        if len(voiced) >= 2:
            vals = [f.fw_counts.get(w, 0) / f.n_words for f in voiced]
            m = sum(vals) / len(vals)
            fw_std[w] = math.sqrt(sum((v - m) ** 2 for v in vals) / (len(vals) - 1))
        else:
            fw_std[w] = 0.0
    return Baseline(pooled=pooled, per_sample=per, fw_mean=fw_mean, fw_std=fw_std)


# --------------------------------------------------------------------------- distances


def _rel_diff(a: float, b: float, floor: float) -> float:
    """|a-b| / (|a|+|b|+floor) — bounded in [0,1), symmetric, tolerant of zeros."""
    return abs(a - b) / (abs(a) + abs(b) + floor)


def sentence_length_distance(text: TextFeatures, base: Baseline) -> float:
    b = base.pooled
    if not text.sentence_lengths or not b.sentence_lengths:
        return 1.0 if (text.sentence_lengths or b.sentence_lengths) else 0.0
    h1, h2 = text.sentence_histogram(), b.sentence_histogram()
    tv = 0.5 * sum(abs(x - y) for x, y in zip(h1, h2, strict=True))  # total variation in [0,1]
    d_mean = _rel_diff(text.mean_sentence_len, b.mean_sentence_len, 2.0)
    d_std = _rel_diff(text.std_sentence_len, b.std_sentence_len, 3.0)
    return min(1.0, 0.5 * tv + 0.35 * d_mean + 0.15 * d_std)


def punctuation_distance(text: TextFeatures, base: Baseline) -> float:
    """Mean bounded relative difference of per-100-word rates over the marks either side uses.

    The floor (1.5 per 100 words) keeps a single stray mark in a short text from dominating.
    """
    r1, r2 = text.punctuation_rates(), base.pooled.punctuation_rates()
    marks = [m for m in PUNCTUATION_MARKS if r1[m] > 0 or r2[m] > 0]
    if not marks:
        return 0.0
    # weight by how prominent the mark is overall so ',' and '.' matter more than ';'
    weights = {m: math.sqrt(r1[m] + r2[m]) for m in marks}
    tot = sum(weights.values())
    return sum(weights[m] * _rel_diff(r1[m], r2[m], 1.5) for m in marks) / tot


def burrows_delta(text: TextFeatures, base: Baseline, top_n: int = 60) -> float:
    """Burrows' Delta: mean |z| over the baseline's most frequent function words.

    z = (f_text - mu_base) / sigma, where sigma combines the baseline's between-sample spread
    with the binomial sampling noise expected for a text of this length
    (sqrt(mu(1-mu)/n_text)). The noise term keeps short texts and 1-sample baselines from
    producing huge z-scores, and makes Delta ≈ 0 for identical text.
    """
    if text.n_words == 0:
        return float("inf") if base.pooled.n_words else 0.0
    ranked = sorted(FUNCTION_WORDS, key=lambda w: (-base.fw_mean[w], w))
    vocab = [w for w in ranked[:top_n] if base.fw_mean[w] > 0]
    # also include function words the text leans on heavily but the baseline never uses
    tf = text.function_word_freqs()
    extra = sorted((w for w in FUNCTION_WORDS if tf[w] > 0 and base.fw_mean[w] == 0), key=lambda w: (-tf[w], w))
    vocab += extra[:10]
    if not vocab:
        return 0.0
    n_t, n_b = text.n_words, max(base.pooled.n_words, 1)
    zs = []
    for w in vocab:
        mu = base.fw_mean[w]
        # Laplace-ish prior on mu for noise so unseen words don't get sigma=0
        mu_s = (base.pooled.fw_counts.get(w, 0) + 0.5) / (n_b + 1)
        noise = math.sqrt(mu_s * (1 - mu_s) / n_t + mu_s * (1 - mu_s) / n_b)
        sigma = math.sqrt(base.fw_std[w] ** 2 + noise**2)
        zs.append(abs(tf[w] - mu) / sigma)
    return sum(zs) / len(zs)


def function_word_density_z(text: TextFeatures, base: Baseline) -> float:
    """|z| of the share of tokens that are function words (dense formal prose ≈ 0.55-0.65,
    terse chat ≈ 0.3-0.4). sigma = binomial noise at the text's length + between-sample spread.
    Complements Delta, which can't see *absence* well in short texts."""
    if text.n_words == 0:
        return float("inf") if base.pooled.n_words else 0.0
    p_t = sum(text.fw_counts.values()) / text.n_words
    p_b = sum(base.pooled.fw_counts.values()) / max(base.pooled.n_words, 1)
    voiced = [f for f in base.per_sample if f.n_words > 0]
    between = 0.0
    if len(voiced) >= 2:
        vals = [sum(f.fw_counts.values()) / f.n_words for f in voiced]
        m = sum(vals) / len(vals)
        between = sum((v - m) ** 2 for v in vals) / (len(vals) - 1)
    q = min(max(p_b, 0.05), 0.95)
    sigma = math.sqrt(q * (1 - q) / text.n_words + between)
    return abs(p_t - p_b) / sigma


def function_word_distance(text: TextFeatures, base: Baseline) -> float:
    """0.6 * Delta + 0.4 * function-word density, each mapped to [0,1] by 1 - exp(-z/2).

    On the calibration fixture same-author Delta ≈ 0.35-0.75; a different voice ≈ 0.7-1.0
    (and higher on longer texts). Density |z| ≈ 0-1 same-author, 1.5-4 different voice.
    """
    delta = burrows_delta(text, base)
    dz = function_word_density_z(text, base)
    if math.isinf(delta) or math.isinf(dz):
        return 1.0
    return 0.6 * (1.0 - math.exp(-delta / 2.0)) + 0.4 * (1.0 - math.exp(-dz / 2.0))


def trigram_distance(text: TextFeatures, base: Baseline, top_n: int = 300) -> float:
    """Cosine distance between character 3-gram frequency profiles (baseline top-N ∪ text top-N)."""
    a, b = text.trigrams, base.pooled.trigrams
    if not a or not b:
        return 1.0 if (a or b) else 0.0
    keys = {k for k, _ in b.most_common(top_n)} | {k for k, _ in a.most_common(top_n)}
    na, nb = sum(a.values()), sum(b.values())
    va = [a.get(k, 0) / na for k in keys]
    vb = [b.get(k, 0) / nb for k in keys]
    dot = sum(x * y for x, y in zip(va, vb, strict=True))
    norm = math.sqrt(sum(x * x for x in va)) * math.sqrt(sum(y * y for y in vb))
    return 1.0 - dot / norm if norm else 1.0


def lexical_distance(text: TextFeatures, base: Baseline) -> float:
    """Blend of vocabulary richness, word length, contractions, casing and char 3-grams."""
    b = base.pooled
    if text.n_words == 0:
        return 1.0 if b.n_words else 0.0
    d_ttr = _rel_diff(text.mattr, b.mattr, 0.2)
    d_wlen = _rel_diff(text.avg_word_len, b.avg_word_len, 1.0)
    d_contr = _rel_diff(text.contraction_rate, b.contraction_rate, 0.04)
    d_case = abs(text.lowercase_start_rate - b.lowercase_start_rate)
    li_t, li_b = text.lowercase_i_rate, b.lowercase_i_rate
    if li_t is not None and li_b is not None:
        d_case = 0.7 * d_case + 0.3 * abs(li_t - li_b)
    d_tri = trigram_distance(text, base)
    return min(
        1.0,
        0.15 * d_ttr + 0.20 * d_wlen + 0.15 * d_contr + 0.20 * d_case + 0.30 * d_tri,
    )
