"""Deterministic style extractor: ``extract(text) -> Extraction``.

Pure python, no models, no network. It captures *cadence*, not meaning:

1. ``clean_message`` drops quoted reply chains and signatures so only the author's words count.
2. ``segment`` splits into sentences (abbreviations, URLs, emails, emoji, Slack-style line
   breaks and bullet lists aware).
3. Per sentence: tokens (contractions kept; numbers/URLs count for length but are not Words),
   FOLLOWED_BY bigrams (numbers/URLs break the chain), openers (1–3 words), discourse markers,
   punctuation, casing, and a ``SentenceStructure`` signature.

Signature format — ``LEN|<n>c|<skeleton>|<opener>|<end>``, e.g. ``M|2c|,—|conj|.``:

- ``LEN``: token-count bucket ``XS`` (1–3) ``S`` (4–8) ``M`` (9–15) ``L`` (16–25) ``XL`` (26+)
- ``<n>c``: clause estimate 1–4 (4 = four or more): 1 + internal ``; : — (`` + subordinators
  + coordinators that follow a comma or introduce a new subject
- ``<skeleton>``: internal punctuation in order, consecutive repeats collapsed, max 4,
  from ``, ; : — ( " …``; ``_`` when none
- ``<opener>``: class of the first word — ``list`` (bullet item), ``conj``, ``disc`` (discourse
  marker), ``greet``, ``pron``, ``wh``, ``aux`` (question-style inversion), ``det``, ``num``, ``word``
- ``<end>``: ``.`` ``!`` ``!!`` ``?`` ``?!`` ``…`` ``e`` (emoji) or ``_`` (no terminal mark)
"""
from __future__ import annotations

import itertools
import re
from collections import Counter

from backend.ingestion.text import (
    AMBIGUOUS_TRANSITIONS,
    EMOJI_RE,
    EXPANDED_FORMS,
    FUNCTION_WORDS,
    TRANSITIONS,
    Token,
    clean_message,
    is_contraction,
    mask_protected,
    segment,
    token_spans,
)
from backend.ingestion.types import Extraction

_CONJ = frozenset({"and", "but", "or", "so", "yet", "plus", "nor", "because", "cause", "cos"})
_DISC = frozenset(
    {"anyway", "anyways", "honestly", "ok", "okay", "well", "actually", "basically", "btw", "also",
     "like", "tbh", "imo", "ngl", "fwiw", "lol", "haha", "yeah", "yep", "nope", "sure", "alright",
     "right", "oh", "ah", "hmm", "um", "uh", "frankly", "admittedly", "fortunately", "unfortunately",
     "luckily", "sadly", "however", "meanwhile", "still", "then", "now", "quick", "just"}
)
_GREET = frozenset({"hey", "hi", "hello", "yo", "dear", "thanks", "thank", "thx", "ty", "morning",
                    "evening", "hiya", "howdy", "cheers", "greetings", "sup"})
_PRON = frozenset({"i", "we", "you", "they", "he", "she", "it", "this", "that", "these", "those",
                   "there", "here", "i'm", "i've", "i'll", "i'd", "we're", "we've", "we'll", "you're",
                   "it's", "that's", "there's", "they're", "let's", "someone", "everyone"})
_WH = frozenset({"what", "why", "how", "when", "where", "who", "which", "whose", "whom"})
_AUX = frozenset({"can", "could", "would", "will", "should", "shall", "may", "might", "do", "does",
                  "did", "is", "are", "was", "were", "have", "has", "had", "am", "can't", "won't",
                  "don't", "didn't", "isn't", "aren't", "wasn't", "shouldn't", "wouldn't", "couldn't"})
_DET = frozenset({"the", "a", "an", "my", "our", "your", "his", "her", "their", "its", "some",
                  "any", "every", "each", "all", "no", "both"})
_SUBORD = frozenset({"because", "cause", "since", "although", "though", "unless", "whereas",
                     "while", "if", "when", "whenever", "until", "which", "who", "where"})
_COORD = frozenset({"and", "but", "or", "so", "yet"})
_SUBJECTS = frozenset({"i", "we", "you", "they", "he", "she", "it", "there", "that", "this",
                       "i'm", "i'll", "i've", "i'd", "we're", "we'll", "you're", "it's", "that's",
                       "they're", "there's"})

_SKELETON_MARKS = {",": ",", ";": ";", ":": ":", "—": "—", "(": "(", '"': '"', "…": "…"}
_DASH_RE = re.compile(r"—|–|(?<!-)--(?!-)|(?<=\s)-(?=\s)")
_ELLIPSIS_RE = re.compile(r"…|\.{2,}")
_QUOTE_RE = re.compile(r"[\"“”]")
_CLOSERS = "\"'”’)]"

# multi-word transitions first (longest match wins), as token tuples
_TRANSITION_SEQS: list[tuple[str, ...]] = sorted(
    (tuple(p.split()) for p in TRANSITIONS), key=lambda t: -len(t)
)


def _length_bucket(n: int) -> str:
    if n <= 3:
        return "XS"
    if n <= 8:
        return "S"
    if n <= 15:
        return "M"
    if n <= 25:
        return "L"
    return "XL"


def _opener_class(first: Token | None, bullet: bool) -> str:
    if bullet:
        return "list"
    if first is None:
        return "word"
    w = first.text
    if first.kind == "num":
        return "num"
    if w in _CONJ:
        return "conj"
    if w in _GREET:
        return "greet"
    if w in _DISC:
        return "disc"
    if w in _WH:
        return "wh"
    if w in _AUX:
        return "aux"
    if w in _PRON:
        return "pron"
    if w in _DET:
        return "det"
    return "word"


def _ending(sentence: str) -> tuple[str, str]:
    """(end class, sentence body without the terminal run)."""
    s = sentence.rstrip().rstrip(_CLOSERS).rstrip()
    m = EMOJI_RE.search(s)
    if m and m.end() == len(s):
        return "e", s[: m.start()]
    m = re.search(r"[.!?…]+$", s)
    if not m:
        return "_", s
    run = m.group(0)
    body = s[: m.start()]
    if "?" in run and "!" in run:
        return "?!", body
    if "!" in run:
        return ("!!" if run.count("!") >= 2 else "!"), body
    if "?" in run:
        return "?", body
    if "…" in run or run.count(".") >= 2:
        return "…", body
    return ".", body


def _count_marks(masked: str) -> tuple[Counter[str], list[tuple[int, str]]]:
    """Punctuation counts for one sentence + positioned marks (for the skeleton)."""
    c: Counter[str] = Counter()
    marks: list[tuple[int, str]] = []
    rest = masked

    def take(pattern: re.Pattern[str], sym: str, *, mark: str | None = None) -> None:
        nonlocal rest
        for m in pattern.finditer(rest):
            c[sym] += 1
            if mark:
                marks.append((m.start(), mark))
        rest = pattern.sub(lambda m: " " * len(m.group(0)), rest)

    take(EMOJI_RE, "emoji")
    take(_ELLIPSIS_RE, "…", mark="…")
    take(_DASH_RE, "—", mark="—")
    for m in re.finditer(r"!+", rest):
        c["!"] += 1
        if len(m.group(0)) >= 2:
            c["!!"] += 1
    c["?"] += len(re.findall(r"\?+", rest))
    for sym in (",", ";", ":", "("):
        for m in re.finditer(re.escape(sym), rest):
            c[sym] += 1
            marks.append((m.start(), sym))
    for m in _QUOTE_RE.finditer(rest):
        c['"'] += 1
        marks.append((m.start(), '"'))
    c["."] += rest.count(".")
    return +c, sorted(marks)


def _skeleton(marks: list[tuple[int, str]], body_len: int) -> str:
    seq: list[str] = []
    for pos, sym in marks:
        if pos >= body_len:
            continue  # part of the terminal run
        if seq and seq[-1] == sym:
            continue
        seq.append(_SKELETON_MARKS[sym])
        if len(seq) >= 4:
            break
    return "".join(seq) or "_"


def _gap_has_break(sentence: str, prev: Token | None, tok: Token) -> bool:
    if prev is None:
        return True
    gap = sentence[prev.end : tok.start]
    return bool(re.search(r"[,;:(—–]|--|\s-\s|[.!?…]", gap))


def _clauses(sentence: str, toks: list[Token], marks: list[tuple[int, str]], body_len: int) -> int:
    n = 1 + sum(1 for pos, sym in marks if sym in (";", ":", "—", "(") and pos < body_len)
    for i, t in enumerate(toks):
        if i == 0:
            continue
        if t.text in _SUBORD:
            n += 1
        elif t.text in _COORD:
            after_comma = "," in sentence[toks[i - 1].end : t.start]
            new_subject = i + 1 < len(toks) and toks[i + 1].text in _SUBJECTS
            if after_comma or new_subject:
                n += 1
    return min(n, 4)


def signature(sentence: str, bullet: bool = False) -> str:
    """Structure signature of one sentence (see module docstring)."""
    toks = token_spans(sentence)
    masked = mask_protected(sentence)
    end, body = _ending(masked)
    _, marks = _count_marks(masked)
    return _signature_from(sentence, toks, marks, len(body), end, bullet)


def _signature_from(sentence, toks, marks, body_len, end, bullet) -> str:
    return "|".join(
        (
            _length_bucket(len(toks)),
            f"{_clauses(sentence, toks, marks, body_len)}c",
            _skeleton(marks, body_len),
            _opener_class(toks[0] if toks else None, bullet),
            end,
        )
    )


def _transitions(sentence: str, toks: list[Token]) -> Counter[str]:
    out: Counter[str] = Counter()
    words = [t.text for t in toks]
    i = 0
    while i < len(words):
        hit = None
        for seq in _TRANSITION_SEQS:
            if tuple(words[i : i + len(seq)]) == seq:
                hit = seq
                break
        if hit is None:
            i += 1
            continue
        phrase = " ".join(hit)
        if len(hit) == 1 and phrase in AMBIGUOUS_TRANSITIONS:
            prev = toks[i - 1] if i else None
            at_end = i == len(words) - 1 and phrase == "though"
            if not (_gap_has_break(sentence, prev, toks[i]) or at_end):
                i += 1
                continue
        out[phrase] += 1
        i += len(hit)
    return out


def extract(text: str) -> Extraction:
    ex = Extraction()
    cleaned = clean_message(text)
    if not cleaned:
        return ex
    words: Counter[str] = Counter()
    bigrams: Counter[tuple[str, str]] = Counter()
    structures: Counter[str] = Counter()
    wstruct: Counter[tuple[str, str]] = Counter()
    openers: Counter[str] = Counter()
    opener_ngrams: Counter[str] = Counter()
    transitions: Counter[str] = Counter()
    punct: Counter[str] = Counter()

    for seg in segment(cleaned, keep_tokenless=True):
        sent = seg.text
        masked = mask_protected(sent)
        counts, marks = _count_marks(masked)
        punct.update(counts)
        toks = token_spans(sent)
        if not toks:
            continue  # e.g. a lone "👍": punctuation counted, no sentence
        end, body = _ending(masked)
        sig = _signature_from(sent, toks, marks, len(body), end, seg.bullet)
        ex.sentence_lengths.append(len(toks))
        structures[sig] += 1

        wtoks = [t for t in toks if t.kind == "word"]
        words.update(t.text for t in wtoks)
        for a, b in itertools.pairwise(toks):
            if a.kind == "word" and b.kind == "word":
                bigrams[(a.text, b.text)] += 1
        trans = _transitions(sent, toks)
        transitions.update(trans)

        first = toks[0]
        if first.kind == "word":
            openers[first.text] += 1
            for n in (2, 3):
                head = toks[:n]
                if len(head) == n and all(t.kind == "word" for t in head) and not any(
                    sent[x.end : y.start].strip() for x, y in itertools.pairwise(head)
                ):
                    opener_ngrams[" ".join(t.text for t in head)] += 1

        # word -> structure links for cadence-bearing words: opener, function words, markers
        linked = {t.text for t in wtoks if t.text in FUNCTION_WORDS}
        if first.kind == "word":
            linked.add(first.text)
        linked.update(w for p in trans for w in p.split())
        for w in linked:
            wstruct[(w, sig)] += 1

        first_alpha = next((ch for ch in sent if ch.isalpha()), "")
        if first_alpha.islower():
            ex.lower_starts += 1
        elif first_alpha.isupper():
            ex.upper_starts += 1
        for t in wtoks:
            if t.text == "i" or t.text.startswith("i'"):
                if t.raw[0] == "i":
                    ex.lower_i += 1
                else:
                    ex.upper_i += 1

    ex.words = dict(words)
    ex.bigrams = dict(bigrams)
    ex.structures = dict(structures)
    ex.word_structures = {k: v for k, v in wstruct.items() if k[0] in words}
    ex.punctuation = dict(punct)
    ex.openers = dict(openers)
    ex.opener_ngrams = dict(opener_ngrams)
    ex.transitions = dict(transitions)
    ex.contractions = sum(n for w, n in words.items() if is_contraction(w))
    ex.expansions = sum(n for bg, n in bigrams.items() if bg in EXPANDED_FORMS)
    return ex
