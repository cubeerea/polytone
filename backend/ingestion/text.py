"""Shared text utilities: message cleaning, sentence segmentation, tokenizing, word lists.

Everything here is pure python, deterministic and offline.

Pipeline used by ``extract``::

    clean_message(text)      # drop quoted reply chains, forwarded headers, signatures
      → segment(text)        # sentences (bullets / Slack line breaks / abbreviations / URLs aware)
        → token_spans(s)     # word / number / url tokens with char offsets
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ----------------------------------------------------------------------------- word lists
FUNCTION_WORDS: frozenset[str] = frozenset(
    ["a", "an", "the", "and", "or", "but", "if", "so", "of", "to", "in", "on", "at", "by", "for", "with", "from", "up", "down", "out", "over", "under", "about", "into", "onto", "than", "then", "as", "is", "am", "are", "was", "were", "be", "been", "being", "do", "does", "did", "have", "has", "had", "i", "me", "my", "mine", "you", "your", "yours", "he", "him", "his", "she", "her", "hers", "it", "its", "we", "us", "our", "ours", "they", "them", "their", "this", "that", "these", "those", "there", "here", "what", "which", "who", "whom", "whose", "when", "where", "why", "how", "not", "no", "yes", "can", "could", "will", "would", "shall", "should", "may", "might", "must", "just", "very", "really", "also", "too", "all", "any", "some", "each", "every", "both", "either", "neither", "more", "most", "less", "least", "such", "own", "same", "other", "another"]
)

# Common English words that carry little personal style. Excluded (with FUNCTION_WORDS) from
# the distinctive-lexicon ranking so the lexicon surfaces words that are *the user's*.
GENERIC_WORDS: frozenset[str] = frozenset(
    ["get", "got", "gets", "getting", "go", "goes", "going", "gone", "went", "come", "comes", "came", "coming", "make", "makes", "made", "making", "take", "takes", "took", "taken", "know", "knows", "knew", "known", "think", "thinks", "thought", "want", "wants", "wanted", "need", "needs", "needed", "see", "sees", "saw", "seen", "look", "looks", "looked", "say", "says", "said", "tell", "told", "ask", "asked", "let", "lets", "put", "use", "used", "give", "gave", "given", "find", "found", "work", "works", "worked", "working", "call", "called", "try", "tried", "keep", "kept", "feel", "felt", "seem", "seems", "leave", "left", "mean", "means", "meant", "show", "shown", "help", "start", "started", "run", "ran", "thing", "things", "stuff", "time", "times", "day", "days", "week", "weeks", "year", "years", "today", "tomorrow", "yesterday", "tonight", "now", "still", "even", "back", "well", "way", "ways", "lot", "lots", "much", "many", "few", "little", "bit", "kind", "sort", "part", "good", "great", "nice", "new", "old", "big", "small", "right", "sure", "fine", "okay", "ok", "yeah", "yep", "yup", "hey", "hi", "hello", "one", "two", "three", "first", "last", "next", "only", "again", "ever", "never", "always", "often", "sometimes", "maybe", "because", "since", "while", "until", "though", "although", "without", "within", "through", "between", "after", "before", "around", "away", "off", "yet", "already", "soon", "later", "once", "like", "than", "whether", "else", "enough", "quite", "rather", "people", "person", "someone", "something", "anything", "everything", "nothing", "anyone", "everyone", "thanks", "thank", "please", "sorry", "will", "would", "could", "should", "can", "cannot", "also", "very", "really", "just", "able", "done", "being", "been", "doing", "does", "did", "gonna", "wanna", "gotta"]
)

TRANSITIONS: tuple[str, ...] = (
    "for what it's worth", "that being said", "having said that", "long story short",
    "on the other hand", "in other words", "at the same time", "to be honest", "in the meantime",
    "by the way", "for example", "for instance", "as a result", "that said", "in any case",
    "in short", "on that note", "either way", "to be fair", "speaking of", "all in all", "in fact",
    "of course", "after all", "then again", "so yeah", "ok so", "okay so",
    "however", "also", "anyway", "anyways", "so", "but", "plus", "still", "though", "although",
    "besides", "meanwhile", "therefore", "thus", "instead", "otherwise", "honestly", "basically",
    "actually", "btw", "tbh", "imo", "ngl", "fwiw", "additionally", "furthermore", "moreover",
    "finally", "then", "overall", "regardless", "frankly",
)
# connectives that are only discourse markers sentence-initially / after a comma or dash
AMBIGUOUS_TRANSITIONS: frozenset[str] = frozenset(
    {"so", "but", "then", "still", "also", "plus", "though", "finally", "instead", "otherwise"}
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

# ----------------------------------------------------------------------------- regexes
_EMOJI_CHARS = (
    "\U0001F000-\U0001FAFF"  # pictographs, emoticons, transport, symbols & pictographs ext.
    "☀-➿"  # misc symbols, dingbats
    "⬀-⯿⌀-⏿"
)
_EMOJI_JOIN = "️‍\U0001F3FB-\U0001F3FF"  # variation selector, ZWJ, skin tones
EMOJI_RE = re.compile(
    rf"[{_EMOJI_CHARS}][{_EMOJI_CHARS}{_EMOJI_JOIN}]*"
    r"|(?<![\w/])(?:[:;=][-'^]?[)(DPpOo3/\\|\]\[*]+|<3|\^_?\^)(?![\w/])"
)
URL_RE = re.compile(
    r"(?:https?://|www\.)[^\s<>\"]*[^\s<>\".,!?;:)\]'’]"
    r"|[\w.+-]+@[\w-]+(?:\.[\w-]+)+"
    r"|\b(?:[a-z0-9-]+\.)+(?:com|org|net|io|ai|dev|co|app|gov|edu|me|so|ly)(?:/[^\s]*[^\s.,!?;:)])?\b",
    re.IGNORECASE,
)
# word (letters, inner apostrophes kept: don't, y'all) | number (3, 3.5, 10:30, 3pm, 2nd)
_TOKEN_RE = re.compile(
    r"\d+(?:[.,:]\d+)*[^\W\d_]*"  # number
    r"|(?:[^\W\d_]\.){2,}(?![^\W\d_])"  # dotted abbreviation: e.g. / p.m. / U.S.
    r"|[^\W\d_][^\W_]*(?:['’][^\W\d_]+)*"  # word (may contain digits after a letter: q3, mp3)
)
_BULLET_RE = re.compile(r"^\s*(?:[-*•·◦‣▪]|\d{1,2}[.)]|\[[ xX]?\])\s+")
_TERM_RE = re.compile(r"[.!?…]+")

_TITLE_ABBREV = frozenset(
    ["mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "mt", "vs", "fig", "cf", "approx", "dept", "est", "sen", "rep", "gov", "gen", "col", "lt", "capt", "sgt", "no"]
)
_END_ABBREV = frozenset(["etc", "inc", "ltd", "co", "corp", "al"])  # may also end a sentence


# ----------------------------------------------------------------------------- cleaning
_REPLY_HEADER_RE = re.compile(r"^\s*on\b.{0,250}\bwrote:\s*$", re.IGNORECASE)
_NAME_WROTE_RE = re.compile(r"^.{0,120}(?:<[^>\s]+@[^>\s]+>|\S+@\S+)\s*wrote:\s*$", re.IGNORECASE)
_FWD_RE = re.compile(r"^-{2,}\s*(?:original message|forwarded message)\s*-{2,}\s*$", re.IGNORECASE)
_RULE_RE = re.compile(r"^_{8,}\s*$")
_HEADER_LINE_RE = re.compile(r"^(?:from|sent|to|cc|subject|date):\s", re.IGNORECASE)
_MOBILE_SIG_RE = re.compile(
    r"^(?:sent from (?:my )?\S.*|sent (?:via|with) \S.*|get outlook for \S.*|"
    r"sent from (?:yahoo|proton|outlook|mail)\b.*)$",
    re.IGNORECASE,
)


def clean_message(text: str) -> str:
    """Keep only the author's own words: drop quoted reply chains ("On ... wrote:", ``>`` lines,
    forwarded/Outlook header blocks) and signatures ("--" delimiter, "Sent from my iPhone")."""
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    t = t.replace("\ufeff", "").replace("\u200b", "").replace("\u00a0", " ")
    lines = t.split("\n")
    out: list[str] = []
    for i, ln in enumerate(lines):
        s = ln.strip()
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if _REPLY_HEADER_RE.match(s) or _NAME_WROTE_RE.match(s):
            break
        if s.lower().startswith("on ") and nxt.lower().endswith("wrote:") and len(s) + len(nxt) < 300:
            break  # header wrapped over two lines
        if ln.rstrip() in ("--", "-- ") or s == "--":
            break  # RFC 3676 signature delimiter
        if _FWD_RE.match(s) or _RULE_RE.match(s):
            break
        if _HEADER_LINE_RE.match(s) and _HEADER_LINE_RE.match(nxt):
            break  # Outlook-style quoted header block
        if len(s) < 60 and _MOBILE_SIG_RE.match(s):
            break
        if s.startswith(">"):
            continue
        out.append(ln)
    return "\n".join(out).strip()


# ----------------------------------------------------------------------------- segmentation
@dataclass(frozen=True)
class Segment:
    text: str
    bullet: bool = False


def mask_protected(text: str) -> str:
    """Same-length copy of ``text`` with URLs / emails replaced by NUL characters (their dots
    and colons must not split sentences, tokenize as words, or count as punctuation)."""
    return URL_RE.sub(lambda m: "\x00" * len(m.group(0)), text)


def _is_abbrev(masked: str, dot_at: int, next_char: str) -> bool:
    """True if the single '.' at ``dot_at`` ends an abbreviation, not a sentence."""
    m = re.search(r"([A-Za-z][A-Za-z.]*)$", masked[:dot_at])
    if not m:
        return False
    word = m.group(1)
    low = word.lower().rstrip(".")
    if "." in word:  # dotted abbreviation: e.g. / i.e. / U.S. / a.m.
        if low in ("e.g", "i.e", "cf"):
            return True
        return not next_char.isupper()
    if low in _TITLE_ABBREV:
        return True
    if low in _END_ABBREV:
        return not next_char.isupper()
    if len(word) == 1 and word.isupper() and word != "I":  # initials: "J. Smith"
        return next_char.isupper()
    return False


def _split_chunk(chunk: str) -> list[str]:
    masked = mask_protected(chunk)
    out: list[str] = []
    start = 0
    pat = re.compile(rf"(?:[.!?…]+|(?:{EMOJI_RE.pattern}))[\"'”’)\]]*(?=\s+\S)")
    for m in pat.finditer(masked):
        end = m.end()
        token = m.group(0).rstrip("\"'”’)]")
        nxt_m = re.compile(r"\s+(\S)").match(masked, end)
        nxt = nxt_m.group(1) if nxt_m else ""
        if token == "." and _is_abbrev(masked, m.start(), nxt):
            continue
        if EMOJI_RE.fullmatch(token) and not re.match(r"\s+[^\W\d_]", masked[end:]):
            continue  # emoji followed by a number / symbol: not a boundary
        piece = chunk[start:end].strip()
        if piece:
            out.append(piece)
        start = end
    tail = chunk[start:].strip()
    if tail:
        out.append(tail)
    return out


def _lines_to_chunks(text: str) -> list[Segment]:
    """Each line is its own unit (Slack-style line breaks, bullets), except hard-wrapped
    email lines (long line, no terminal punctuation, next line starts lowercase)."""
    chunks: list[Segment] = []
    cur: str | None = None
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            if cur is not None:
                chunks.append(Segment(cur))
                cur = None
            continue
        bm = _BULLET_RE.match(line)
        if bm:
            if cur is not None:
                chunks.append(Segment(cur))
                cur = None
            item = line[bm.end():].strip()
            if item:
                chunks.append(Segment(item, bullet=True))
            continue
        if (
            cur is not None
            and len(cur.split("\n")[-1]) >= 60
            and not re.search(r"[.!?…:]$", cur)
            and (line[0].islower() or line[0].isdigit())
        ):
            cur = cur + " " + line
            continue
        if cur is not None:
            chunks.append(Segment(cur))
        cur = line
    if cur is not None:
        chunks.append(Segment(cur))
    return chunks


def segment(text: str, *, keep_tokenless: bool = False) -> list[Segment]:
    """Split (already cleaned) text into sentences. Bullet items are flagged ``bullet=True``.
    Token-less pieces (a lone "👍") are dropped unless ``keep_tokenless``."""
    out: list[Segment] = []
    for ch in _lines_to_chunks(text or ""):
        for s in _split_chunk(ch.text):
            if keep_tokenless or _TOKEN_RE.search(mask_protected(s)) or URL_RE.search(s):
                out.append(Segment(s, ch.bullet))
    return out


def split_sentences(text: str) -> list[str]:
    return [s.text for s in segment(text)]


# ----------------------------------------------------------------------------- tokens
@dataclass(frozen=True)
class Token:
    text: str  # lower-cased, curly apostrophes normalized
    kind: str  # "word" | "num" | "url"
    start: int
    end: int
    raw: str


def token_spans(sentence: str) -> list[Token]:
    """Word / number / url tokens with offsets. URLs and emails are one ``url`` token."""
    toks: list[Token] = []
    for m in URL_RE.finditer(sentence):
        toks.append(Token("<url>", "url", m.start(), m.end(), m.group(0)))
    masked = mask_protected(sentence)
    for m in _TOKEN_RE.finditer(masked):
        raw = sentence[m.start():m.end()]
        kind = "num" if raw[0].isdigit() else "word"
        toks.append(Token(raw.lower().replace("’", "'"), kind, m.start(), m.end(), raw))
    toks.sort(key=lambda t: t.start)
    return toks


def tokenize(text: str) -> list[str]:
    """Lower-cased word + number tokens (URLs/emails dropped); curly apostrophes normalized."""
    return [t.lower().replace("’", "'") for t in _TOKEN_RE.findall(mask_protected(text or ""))]


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


def containment(inner: set, outer: set) -> float:
    """Share of ``inner``'s shingles that also occur in ``outer``."""
    if not inner or not outer:
        return 0.0
    return len(inner & outer) / len(inner)
