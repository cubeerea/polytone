"""Data types shared by ingestion (producer) and the graph store (consumer).

``extract(text) -> Extraction`` fills one of these per human sample; the graph store
merges it into Neo4j via ``GraphStore.write_extraction(extraction, persona)``.

All words are lower-cased tokens (apostrophes kept, e.g. ``"don't"``). Counts are
occurrences within the one sample. Pure numbers and URLs/emails count towards sentence
length but never become Word nodes.

Fields added in Phase 2 (all defaulted, so older dicts still load): ``opener_ngrams``,
``contractions``, ``expansions``, ``lower_i``, ``upper_i``.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Extraction:
    # word -> count
    words: dict[str, int] = field(default_factory=dict)
    # (word_a, word_b) -> count, adjacent tokens inside one sentence (Word FOLLOWED_BY Word)
    bigrams: dict[tuple[str, str], int] = field(default_factory=dict)
    # structure signature -> count (SentenceStructure)
    structures: dict[str, int] = field(default_factory=dict)
    # (word, signature) -> count (Word PART_OF_STRUCTURE SentenceStructure)
    word_structures: dict[tuple[str, str], int] = field(default_factory=dict)
    # punctuation symbol -> count (PunctuationMark). Canonical symbols: "," "." ";" ":" "(" '"'
    # "!" / "?" (one per run, so "!!!" counts once), "!!" (runs of 2+ "!"),
    # "—" (em/en dash, "--", spaced " - "), "…" (ellipsis, also "..."),
    # "emoji" (one per run of emoji / emoticons like ":)")
    punctuation: dict[str, int] = field(default_factory=dict)
    # token count of every sentence, in order
    sentence_lengths: list[int] = field(default_factory=list)
    # first word (lower-cased) of each sentence -> count
    openers: dict[str, int] = field(default_factory=dict)
    # transition word/phrase (e.g. "however", "by the way") -> count
    transitions: dict[str, int] = field(default_factory=dict)
    # sentences whose first letter is lower-case / upper-case (casing habit)
    lower_starts: int = 0
    upper_starts: int = 0
    # 2- and 3-word sentence openers ("just wanted", "quick update on") -> count
    opener_ngrams: dict[str, int] = field(default_factory=dict)
    # contraction tokens (don't, I'm) / expandable full forms (do not, I am)
    contractions: int = 0
    expansions: int = 0
    # the pronoun written "i" vs "I"
    lower_i: int = 0
    upper_i: int = 0

    @property
    def sentence_count(self) -> int:
        return len(self.sentence_lengths)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["bigrams"] = [[a, b, n] for (a, b), n in self.bigrams.items()]
        d["word_structures"] = [[w, s, n] for (w, s), n in self.word_structures.items()]
        return d

    @classmethod
    def from_any(cls, obj: Extraction | dict[str, Any]) -> Extraction:
        """Accept an Extraction or a dict (bigram keys may be tuples, lists or 'a b' strings)."""
        if isinstance(obj, Extraction):
            return obj
        if not isinstance(obj, dict):
            raise TypeError(f"cannot build Extraction from {type(obj)!r}")

        def pairs(v: Any) -> dict[tuple[str, str], int]:
            out: dict[tuple[str, str], int] = {}
            if isinstance(v, dict):
                items = v.items()
            else:  # list of [a, b, n]
                items = (((x[0], x[1]), x[2]) for x in (v or []))
            for k, n in items:
                if isinstance(k, str):
                    a, _, b = k.partition(" ") if "\t" not in k else k.partition("\t")
                    k = (a, b)
                out[(str(k[0]), str(k[1]))] = out.get((str(k[0]), str(k[1])), 0) + int(n)
            return out

        return cls(
            words={str(k): int(v) for k, v in (obj.get("words") or {}).items()},
            bigrams=pairs(obj.get("bigrams")),
            structures={str(k): int(v) for k, v in (obj.get("structures") or {}).items()},
            word_structures=pairs(obj.get("word_structures")),
            punctuation={str(k): int(v) for k, v in (obj.get("punctuation") or {}).items()},
            sentence_lengths=[int(x) for x in (obj.get("sentence_lengths") or [])],
            openers={str(k): int(v) for k, v in (obj.get("openers") or {}).items()},
            transitions={str(k): int(v) for k, v in (obj.get("transitions") or {}).items()},
            lower_starts=int(obj.get("lower_starts") or 0),
            upper_starts=int(obj.get("upper_starts") or 0),
            opener_ngrams={str(k): int(v) for k, v in (obj.get("opener_ngrams") or {}).items()},
            contractions=int(obj.get("contractions") or 0),
            expansions=int(obj.get("expansions") or 0),
            lower_i=int(obj.get("lower_i") or 0),
            upper_i=int(obj.get("upper_i") or 0),
        )
