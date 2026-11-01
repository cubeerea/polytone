"""Large, repetitive text must not overload the style graph.

The graph stores counts, not occurrences, and ``graph/trim.py`` bounds the distinct rows a sample
can write and the FOLLOWED_BY fan-out of common words ("the", "and"). These tests feed a large
text (tens of thousands of words, a few common words repeated thousands of times, a long tail of
rare words) and check that the graph stays bounded while the counts it keeps are exact.
Neo4j versions of the same checks live in ``test_neo4j.py``.
"""
import itertools
import string
import time
from collections import Counter

from backend.graph import trim
from backend.graph.store import InMemoryGraphStore
from backend.ingestion.extract import extract
from backend.ingestion.text import tokenize

COMMON = ["the", "and", "of", "in", "is", "it", "that", "for"]  # the words big_text repeats


def rare_words(n: int, prefix: str = "") -> list[str]:
    """``n`` distinct letter-only words (digits would make them numbers, not Words)."""
    letters = string.ascii_lowercase
    out = []
    for size in itertools.count(3):
        for combo in itertools.product(letters, repeat=size):
            out.append(prefix + "".join(combo))
            if len(out) == n:
                return out
    return out  # pragma: no cover


def big_text(n_sentences: int = 4000, n_rare: int = 6000, prefix: str = "") -> str:
    """Redundant, repeating text: every sentence is built from the common words plus two rare
    words, and the same few sentences also repeat verbatim many times."""
    rare = rare_words(n_rare, prefix)
    lines = []
    for i in range(n_sentences):
        x, y = rare[(2 * i) % n_rare], rare[(2 * i + 1) % n_rare]
        lines.append(f"the {x} and the {y} is in the {x} of it, and that is it for the {y}.")
        if i % 10 == 0:
            lines.append("the the and and the and of the.")  # stutters / pure redundancy
    return " ".join(lines)


def degrees(store: InMemoryGraphStore) -> tuple[int, int]:
    return (max((len(v) for v in store.succ.values()), default=0),
            max((len(v) for v in store.pred.values()), default=0))


def test_trim_keeps_exact_counts_for_the_rows_it_keeps():
    text = big_text()
    full = extract(text)
    t = trim.trim_extraction(full)
    assert len(full.words) > trim.MAX_WORDS  # the sample really is over the limits
    assert len(t.words) <= trim.MAX_WORDS
    assert len(t.bigrams) <= trim.MAX_BIGRAMS
    assert len(t.word_structures) <= trim.MAX_WORD_STRUCTURES
    assert len(t.structures) <= trim.MAX_STRUCTURES
    for m in (t.openers, t.opener_ngrams, t.transitions):
        assert len(m) <= trim.MAX_KEYED
    # common words are the most frequent, so they are always kept, with their full counts
    toks = Counter(tokenize(text))
    for w in COMMON:
        assert t.words[w] == full.words[w] == toks[w]
    assert t.words["the"] > 10_000
    # kept rows are a subset with unchanged counts; every bigram's words are kept words
    assert all(full.words[w] == n for w, n in t.words.items())
    assert all(full.bigrams[bg] == n for bg, n in t.bigrams.items())
    assert all(a in t.words and b in t.words for a, b in t.bigrams)
    fan_out, fan_in = Counter(a for a, _ in t.bigrams), Counter(b for _, b in t.bigrams)
    assert max(fan_out.values()) <= trim.SAMPLE_FANOUT and max(fan_in.values()) <= trim.SAMPLE_FANOUT
    # the strongest bigram survives
    assert t.bigrams[("the", "and")] == full.bigrams[("the", "and")]
    # sentence statistics are not trimmed
    assert t.sentence_lengths == full.sentence_lengths


def test_large_redundant_text_stays_bounded_in_the_graph():
    text = big_text()
    full = extract(text)
    s = InMemoryGraphStore()
    t0 = time.perf_counter()
    r = s.write_extraction(full, "Default")
    elapsed = time.perf_counter() - t0
    assert elapsed < 5.0, f"one large write took {elapsed:.2f}s"
    assert r["sentences"] == full.sentence_count > 4000
    assert len(s.words) <= trim.MAX_WORDS
    assert len(s.bigrams) <= trim.MAX_BIGRAMS
    assert len(s.word_struct) <= trim.MAX_WORD_STRUCTURES
    assert max(degrees(s)) <= trim.SAMPLE_FANOUT
    # one node per word, weight = the count (not a node / edge per occurrence)
    assert s.words["the"] == full.words["the"]
    assert s.bigrams[("the", "and")] == full.bigrams[("the", "and")]
    c = s.get_constraints("Default")
    assert c["sample_count"] == 1 and c["lexicon"]  # still a usable style
    assert c["avg_sentence_len"] > 0


def test_repeating_the_same_text_grows_counts_not_the_graph():
    ex = extract(big_text(n_sentences=1500, n_rare=2000))
    s = InMemoryGraphStore()
    s.write_extraction(ex, "Default")
    size = (len(s.words), len(s.bigrams), len(s.structures), len(s.word_struct))
    the, and_ = s.words["the"], s.bigrams[("the", "and")]
    for _ in range(9):
        s.write_extraction(ex, "Default")
    assert (len(s.words), len(s.bigrams), len(s.structures), len(s.word_struct)) == size
    assert s.words["the"] == 10 * the and s.bigrams[("the", "and")] == 10 * and_
    ctx = s.contexts["Default"]
    assert ctx["sample_count"] == 10
    assert all(len(ctx[k]) <= trim.MAX_KEYED for k in ("opener_keys", "opener_phrase_keys", "transition_keys"))


def test_common_word_fanout_is_capped_graph_wide():
    """Across many samples "the" meets thousands of different words; it keeps only the
    strongest GRAPH_FANOUT edges per direction, and the frequent ones survive."""
    rare = rare_words(trim.GRAPH_FANOUT * 4)
    s = InMemoryGraphStore()
    for i in range(0, len(rare), 50):
        batch = rare[i : i + 50]
        body = " ".join(f"the {w} and {w} the." for w in batch)
        s.write_extraction(extract(body + " the cat and the cat and the cat."), "Default")
        out_deg, in_deg = len(s.succ["the"]), len(s.pred["the"])
        assert out_deg <= trim.GRAPH_FANOUT and in_deg <= trim.GRAPH_FANOUT
    assert len(s.succ["the"]) == trim.GRAPH_FANOUT
    assert ("the", "cat") in s.bigrams and s.bigrams[("the", "cat")] > 1
    # adjacency and the bigram table agree after pruning
    assert {(a, b) for a, bs in s.succ.items() for b in bs} == set(s.bigrams)
    assert {(a, b) for b, as_ in s.pred.items() for a in as_} == set(s.bigrams)
    assert s.get_constraints("Default")["top_bigrams"]


def test_wipe_clears_fanout_index():
    s = InMemoryGraphStore()
    s.write_extraction(extract("the cat and the dog."), "Default")
    s.wipe()
    assert not s.succ and not s.pred and not s.bigrams


def test_extraction_scales_linearly_with_text_size():
    """Sentence segmentation used to rescan the whole prefix at every '.', so a 300 KB text took
    ~12 s. Four times the text must cost about four times the time, not sixteen."""
    small, large = big_text(n_sentences=500), big_text(n_sentences=2000)

    def best(text: str) -> float:
        times = []
        for _ in range(3):
            t0 = time.perf_counter()
            extract(text)
            times.append(time.perf_counter() - t0)
        return min(times)

    t_small, t_large = best(small), best(large)
    assert t_large < 8 * t_small + 0.05, f"{t_small:.3f}s -> {t_large:.3f}s"
    assert t_large < 2.0
