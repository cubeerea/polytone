"""Placeholder extractor: ``extract(text) -> Extraction``.

Pure python, deterministic, no network. A later phase replaces the structure
signatures with a richer analysis; keep the signature ``extract(text: str) -> Extraction``.
"""
from __future__ import annotations

import itertools
import re
from collections import Counter

from backend.ingestion.text import (
    FUNCTION_WORDS,
    TRANSITIONS,
    split_sentences,
    tokenize,
)
from backend.ingestion.types import Extraction

_PUNCT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("…", re.compile(r"…|\.\.\.")),
    ("—", re.compile(r"—|–|(?<!-)--(?!-)|\s-\s")),
    ("!", re.compile(r"!")),
    ("?", re.compile(r"\?")),
    (",", re.compile(r",")),
    (";", re.compile(r";")),
    (":", re.compile(r":(?!//)")),
    ("(", re.compile(r"\(")),
    ('"', re.compile(r'"|“')),
]


def _token_class(tok: str) -> str:
    if tok.isdigit():
        return "N"
    if "'" in tok:
        return "C"
    if tok in FUNCTION_WORDS:
        return "F"
    return "W"


def _signature(tokens: list[str], sentence: str) -> str:
    classes = [_token_class(t) for t in tokens[:6]]
    if len(tokens) > 6:
        classes.append("+")
    end = sentence.rstrip()[-1:] if sentence.rstrip() else ""
    end = end if end in ".!?…" else "_"
    return "-".join(classes) + "|" + end


def _count_punct(text: str) -> Counter[str]:
    c: Counter[str] = Counter()
    rest = text
    for sym, pat in _PUNCT_PATTERNS:
        n = len(pat.findall(rest))
        if n:
            c[sym] += n
            rest = pat.sub(" ", rest)
    # periods remaining after ellipses were removed
    n = rest.count(".")
    if n:
        c["."] += n
    return c


def extract(text: str) -> Extraction:
    ex = Extraction()
    words: Counter[str] = Counter()
    bigrams: Counter[tuple[str, str]] = Counter()
    structures: Counter[str] = Counter()
    wstruct: Counter[tuple[str, str]] = Counter()
    openers: Counter[str] = Counter()
    transitions: Counter[str] = Counter()

    for sent in split_sentences(text):
        toks = tokenize(sent)
        if not toks:
            continue
        ex.sentence_lengths.append(len(toks))
        words.update(toks)
        bigrams.update(itertools.pairwise(toks))
        sig = _signature(toks, sent)
        structures[sig] += 1
        for t in set(toks):
            if t not in FUNCTION_WORDS:
                wstruct[(t, sig)] += 1
        openers[toks[0]] += 1
        first_alpha = next((ch for ch in sent if ch.isalpha()), "")
        if first_alpha.islower():
            ex.lower_starts += 1
        elif first_alpha.isupper():
            ex.upper_starts += 1
        joined = " " + " ".join(toks) + " "
        for phrase in TRANSITIONS:
            if " " in phrase:
                n = joined.count(" " + phrase + " ")
            elif phrase in ("so", "but", "then", "still", "also", "plus", "though"):
                # only count ambiguous connectives when they open a sentence
                n = 1 if toks[0] == phrase else 0
            else:
                n = toks.count(phrase)
            if n:
                transitions[phrase] += n

    ex.words = dict(words)
    ex.bigrams = dict(bigrams)
    ex.structures = dict(structures)
    ex.word_structures = dict(wstruct)
    ex.punctuation = dict(_count_punct(text))
    ex.openers = dict(openers)
    ex.transitions = dict(transitions)
    return ex
