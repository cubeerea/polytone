"""Small shared text utilities (tokenizing, sentence splitting, word lists)."""
from __future__ import annotations

import re

FUNCTION_WORDS: frozenset[str] = frozenset(
    ["a", "an", "the", "and", "or", "but", "if", "so", "of", "to", "in", "on", "at", "by", "for", "with", "from", "up", "down", "out", "over", "under", "about", "into", "onto", "than", "then", "as", "is", "am", "are", "was", "were", "be", "been", "being", "do", "does", "did", "have", "has", "had", "i", "me", "my", "mine", "you", "your", "yours", "he", "him", "his", "she", "her", "hers", "it", "its", "we", "us", "our", "ours", "they", "them", "their", "this", "that", "these", "those", "there", "here", "what", "which", "who", "whom", "whose", "when", "where", "why", "how", "not", "no", "yes", "can", "could", "will", "would", "shall", "should", "may", "might", "must", "just", "very", "really", "also", "too", "all", "any", "some", "each", "every", "both", "either", "neither", "more", "most", "less", "least", "such", "own", "same", "other", "another"]
)

TRANSITIONS: tuple[str, ...] = (
    "by the way", "on the other hand", "in other words", "for example", "for instance",
    "as a result", "that said", "in any case", "at the same time", "to be honest", "in short",
    "however", "also", "anyway", "anyways", "so", "but", "plus", "still", "though", "although",
    "besides", "meanwhile", "therefore", "thus", "instead", "otherwise", "honestly", "basically",
    "actually", "btw", "additionally", "furthermore", "moreover", "finally", "then", "overall",
)

# expanded forms that could have been contracted (used for contractions_rate)
EXPANDED_FORMS: frozenset[tuple[str, str]] = frozenset(
    {
        ("do", "not"), ("does", "not"), ("did", "not"), ("is", "not"), ("are", "not"),
        ("was", "not"), ("were", "not"), ("have", "not"), ("has", "not"), ("had", "not"),
        ("will", "not"), ("would", "not"), ("could", "not"), ("should", "not"), ("can", "not"),
        ("i", "am"), ("you", "are"), ("we", "are"), ("they", "are"), ("it", "is"),
        ("that", "is"), ("there", "is"), ("i", "will"), ("you", "will"), ("we", "will"),
        ("i", "have"), ("you", "have"), ("we", "have"), ("i", "would"), ("let", "us"),
    }
)

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z]+)*")
_SENT_RE = re.compile(r"(?<=[.!?…])\s+|\n+")


def tokenize(text: str) -> list[str]:
    """Lower-cased word tokens; curly apostrophes normalized to ``'``."""
    return [t.lower().replace("’", "'") for t in _TOKEN_RE.findall(text)]


def split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENT_RE.split(text or "")]
    return [p for p in parts if p and _TOKEN_RE.search(p)]


_S_CONTRACTIONS = frozenset(
    {"it's", "that's", "there's", "what's", "he's", "she's", "let's", "here's", "who's", "where's"}
)


def is_contraction(word: str) -> bool:
    """True for contractions like don't / I'm / we'll (possessive 's excluded)."""
    if word in _S_CONTRACTIONS:
        return True
    return word.endswith(("n't", "'re", "'m", "'ll", "'ve", "'d"))


def shingles(text: str, n: int = 3) -> set[tuple[str, ...]]:
    toks = tokenize(text)
    if len(toks) < n:
        return {tuple(toks)} if toks else set()
    return {tuple(toks[i : i + n]) for i in range(len(toks) - n + 1)}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
