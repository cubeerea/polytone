"""Cypher templates for Neo4jGraphStore. All parameterized; writes are batched UNWINDs.

Schema (CONTRACTS.md):
  (:Word {key, text, weight, personas, persona_counts})
  (:SentenceStructure {signature, weight})
  (:PunctuationMark {symbol, weight})
  (:IntentContext {name, sample_count, sample_weight, sentence_count, lower_starts, upper_starts,
                   len_sum, len_hist, contractions, expansions,
                   opener_keys, opener_texts, opener_counts,
                   opener_phrase_keys, opener_phrase_texts, opener_phrase_counts,
                   transition_keys, transition_texts, transition_counts})
  (:Word)-[:FOLLOWED_BY {weight, personas, persona_weights}]->(:Word)
  (:Word)-[:PART_OF_STRUCTURE {weight}]->(:SentenceStructure)
  (:SentenceStructure)-[:USED_IN {weight}]->(:IntentContext)
  (:PunctuationMark)-[:USED_IN {weight}]->(:IntentContext)
  (:PolytoneMeta {name: "schema", enc_version,    -- encryption migration marker
                  decay_t0, decay_half_life_s,     -- recency-decay epoch (graph/decay.py)
                  rebase_epoch, rebase_target_t0, rebase_factor, rebase_half_life_s})
  every node / relationship above may carry ``decay_epoch`` (the last rebase applied to it)

Encryption at rest: the user's words never reach Neo4j in plaintext. ``Word.key`` is the blind
index (``idx1:``, unique, what MERGE / MATCH use) and ``Word.text`` the ``enc1:`` ciphertext
(display value, decrypted by the store). IntentContext opener / opener-phrase / transition lists
hold blind indexes in ``*_keys`` (merged by key) and the ciphertexts in the parallel ``*_texts``.
Signatures, punctuation symbols and persona names carry no words and stay plain.
Read ordering ties break on ``key`` (both stores do the same, so results match).

IntentContext.name is the persona. Per-persona weights on Word / FOLLOWED_BY are stored as
two parallel lists (``personas[i]`` ↔ ``persona_counts[i]`` / ``persona_weights[i]``) because
Neo4j properties cannot hold maps and we avoid APOC.

Recency decay (``graph/decay.py``): weights and IntentContext counters are stored in "growing
units" — the store multiplies every increment by ``s(now)`` in Python and passes the result as
``row.n`` / ``$d.*``, so the Cypher below only ever adds parameters (no time arithmetic in Cypher,
identical floats in both stores). ``sample_count`` is the raw integer count; ``sample_weight`` its
decayed twin (missing on contexts written before decay: then it equals ``sample_count``).
"""

SCHEMA = [
    "CREATE CONSTRAINT word_key IF NOT EXISTS FOR (w:Word) REQUIRE w.key IS UNIQUE",
    ("CREATE CONSTRAINT structure_signature IF NOT EXISTS "
    "FOR (s:SentenceStructure) REQUIRE s.signature IS UNIQUE"),
    "CREATE CONSTRAINT punct_symbol IF NOT EXISTS FOR (p:PunctuationMark) REQUIRE p.symbol IS UNIQUE",
    "CREATE CONSTRAINT context_name IF NOT EXISTS FOR (c:IntentContext) REQUIRE c.name IS UNIQUE",
]

# ----------------------------------------------------------------------------- writes
# $d.openers / $d.opener_phrases / $d.transitions are lists of {k: blind index, e: ciphertext, n}.
MERGE_CONTEXT = """
MERGE (c:IntentContext {name: $persona})
ON CREATE SET c.sample_count = 0, c.sample_weight = 0, c.sentence_count = 0, c.lower_starts = 0, c.upper_starts = 0,
              c.len_sum = 0, c.len_hist = [], c.contractions = 0, c.expansions = 0,
              c.opener_keys = [], c.opener_texts = [], c.opener_counts = [],
              c.transition_keys = [], c.transition_texts = [], c.transition_counts = [],
              c.opener_phrase_keys = [], c.opener_phrase_texts = [], c.opener_phrase_counts = []
SET c.sample_weight = coalesce(c.sample_weight, c.sample_count) + $d.sample_weight,
    c.sample_count = c.sample_count + 1,
    c.sentence_count = c.sentence_count + $d.sentence_count,
    c.lower_starts = c.lower_starts + $d.lower_starts,
    c.upper_starts = c.upper_starts + $d.upper_starts,
    c.len_sum = c.len_sum + $d.len_sum,
    c.contractions = c.contractions + $d.contractions,
    c.expansions = c.expansions + $d.expansions,
    c.len_hist = [i IN range(0, size($d.len_hist) - 1) | coalesce(c.len_hist[i], 0) + $d.len_hist[i]],
    c.opener_phrase_keys = coalesce(c.opener_phrase_keys, []),
    c.opener_phrase_counts = coalesce(c.opener_phrase_counts, []),
    c.opener_texts = coalesce(c.opener_texts, []),
    c.opener_phrase_texts = coalesce(c.opener_phrase_texts, []),
    c.transition_texts = coalesce(c.transition_texts, [])
WITH c
SET c.opener_texts = c.opener_texts + [x IN $d.openers WHERE NOT x.k IN c.opener_keys | x.e],
    c.transition_texts = c.transition_texts + [x IN $d.transitions WHERE NOT x.k IN c.transition_keys | x.e],
    c.opener_phrase_texts = c.opener_phrase_texts
                            + [x IN $d.opener_phrases WHERE NOT x.k IN c.opener_phrase_keys | x.e]
WITH c
SET c.opener_keys = c.opener_keys + [x IN $d.openers WHERE NOT x.k IN c.opener_keys | x.k],
    c.transition_keys = c.transition_keys + [x IN $d.transitions WHERE NOT x.k IN c.transition_keys | x.k],
    c.opener_phrase_keys = c.opener_phrase_keys
                           + [x IN $d.opener_phrases WHERE NOT x.k IN c.opener_phrase_keys | x.k]
WITH c
SET c.opener_counts = [i IN range(0, size(c.opener_keys) - 1) |
                         coalesce(c.opener_counts[i], 0)
                         + coalesce(head([x IN $d.openers WHERE x.k = c.opener_keys[i] | x.n]), 0)],
    c.transition_counts = [i IN range(0, size(c.transition_keys) - 1) |
                         coalesce(c.transition_counts[i], 0)
                         + coalesce(head([x IN $d.transitions WHERE x.k = c.transition_keys[i] | x.n]), 0)],
    c.opener_phrase_counts = [i IN range(0, size(c.opener_phrase_keys) - 1) |
                         coalesce(c.opener_phrase_counts[i], 0)
                         + coalesce(head([x IN $d.opener_phrases WHERE x.k = c.opener_phrase_keys[i] | x.n]), 0)]
"""

# rows: {key: blind index, text: ciphertext (used only when the node is created), n}
MERGE_WORDS = """
UNWIND $rows AS row
MERGE (w:Word {key: row.key})
ON CREATE SET w.text = row.text, w.weight = 0, w.personas = [], w.persona_counts = []
SET w.weight = w.weight + row.n
WITH w, row
SET w.personas = CASE WHEN $persona IN w.personas THEN w.personas ELSE w.personas + $persona END
WITH w, row
SET w.persona_counts = [i IN range(0, size(w.personas) - 1) |
    coalesce(w.persona_counts[i], 0) + CASE WHEN w.personas[i] = $persona THEN row.n ELSE 0 END]
"""

MERGE_BIGRAMS = """
UNWIND $rows AS row
MATCH (a:Word {key: row.a})
MATCH (b:Word {key: row.b})
MERGE (a)-[r:FOLLOWED_BY]->(b)
ON CREATE SET r.weight = 0, r.personas = [], r.persona_weights = []
SET r.weight = r.weight + row.n
WITH r, row
SET r.personas = CASE WHEN $persona IN r.personas THEN r.personas ELSE r.personas + $persona END
WITH r, row
SET r.persona_weights = [i IN range(0, size(r.personas) - 1) |
    coalesce(r.persona_weights[i], 0) + CASE WHEN r.personas[i] = $persona THEN row.n ELSE 0 END]
"""

# Supernode guard (graph/trim.py): after a write, each touched word keeps at most $cap outgoing
# ($keys = first words) / incoming ($keys = second words) FOLLOWED_BY edges, highest weight
# first, ties on the other word's key. The COUNT {} check reads the degree store, so words
# under the cap cost one lookup.
PRUNE_FOLLOWERS = """
UNWIND $keys AS k
MATCH (a:Word {key: k})
WHERE COUNT { (a)-[:FOLLOWED_BY]->() } > $cap
MATCH (a)-[r:FOLLOWED_BY]->(b:Word)
WITH a, r ORDER BY r.weight DESC, b.key ASC
WITH a, collect(r) AS rs
UNWIND rs[$cap..] AS x
DELETE x
RETURN count(*) AS n
"""

PRUNE_PREDECESSORS = """
UNWIND $keys AS k
MATCH (b:Word {key: k})
WHERE COUNT { ()-[:FOLLOWED_BY]->(b) } > $cap
MATCH (a:Word)-[r:FOLLOWED_BY]->(b)
WITH b, r ORDER BY r.weight DESC, a.key ASC
WITH b, collect(r) AS rs
UNWIND rs[$cap..] AS x
DELETE x
RETURN count(*) AS n
"""

MERGE_STRUCTURES = """
MATCH (c:IntentContext {name: $persona})
UNWIND $rows AS row
MERGE (s:SentenceStructure {signature: row.sig})
ON CREATE SET s.weight = 0
SET s.weight = s.weight + row.n
MERGE (s)-[r:USED_IN]->(c)
ON CREATE SET r.weight = 0
SET r.weight = r.weight + row.n
"""

MERGE_WORD_STRUCTURES = """
UNWIND $rows AS row
MATCH (w:Word {key: row.w})
MATCH (s:SentenceStructure {signature: row.sig})
MERGE (w)-[r:PART_OF_STRUCTURE]->(s)
ON CREATE SET r.weight = 0
SET r.weight = r.weight + row.n
"""

MERGE_PUNCTUATION = """
MATCH (c:IntentContext {name: $persona})
UNWIND $rows AS row
MERGE (p:PunctuationMark {symbol: row.sym})
ON CREATE SET p.weight = 0
SET p.weight = p.weight + row.n
MERGE (p)-[r:USED_IN]->(c)
ON CREATE SET r.weight = 0
SET r.weight = r.weight + row.n
"""

# ----------------------------------------------------------------------------- persona merge
# A deleted persona's learning moves to another persona ($src → $dst). Global weights
# (Word.weight, FOLLOWED_BY.weight, USED_IN totals) are unchanged; only the attribution moves.
# The no-op SETs take write locks on both contexts, so an ingest into $dst (another thread or
# process) waits for this transaction instead of being overwritten by SET_PERSONA_CONTEXT.
READ_CONTEXT_PAIR = """
MATCH (s:IntentContext {name: $src})
SET s.name = s.name
WITH s
OPTIONAL MATCH (d:IntentContext {name: $dst})
SET d.name = d.name
RETURN properties(s) AS s, properties(d) AS d
"""

# Per-persona lists on Word and FOLLOWED_BY: drop $src's slot, add its count to $dst's slot.
_MOVE_LIST = """
WITH x, [i IN range(0, size(x.personas) - 1) WHERE x.personas[i] = $src][0] AS si
WITH x, coalesce(x.{counts}[si], 0) AS sn,
     [i IN range(0, size(x.personas) - 1) WHERE x.personas[i] <> $src] AS keep
WITH x, sn, [i IN keep | x.personas[i]] AS ps, [i IN keep | coalesce(x.{counts}[i], 0)] AS cs
WITH x, sn, CASE WHEN $dst IN ps THEN ps ELSE ps + $dst END AS ps2,
     CASE WHEN $dst IN ps THEN cs ELSE cs + 0 END AS cs2
SET x.personas = ps2,
    x.{counts} = [i IN range(0, size(ps2) - 1) | cs2[i] + CASE WHEN ps2[i] = $dst THEN sn ELSE 0 END]
"""
MOVE_PERSONA_WEIGHTS = (
    "MATCH (x:Word) WHERE $src IN x.personas" + _MOVE_LIST.replace("{counts}", "persona_counts")
    + "WITH count(*) AS _\n"
    + "MATCH ()-[x:FOLLOWED_BY]->() WHERE $src IN x.personas" + _MOVE_LIST.replace("{counts}", "persona_weights")
)

MOVE_PERSONA_USED_IN = """
MATCH (x)-[r:USED_IN]->(:IntentContext {name: $src})
MERGE (d:IntentContext {name: $dst})
MERGE (x)-[r2:USED_IN]->(d)
ON CREATE SET r2.weight = 0
SET r2.weight = r2.weight + r.weight
DELETE r
"""

# $props = the merged IntentContext properties (store.merged_context)
SET_PERSONA_CONTEXT = """
MERGE (d:IntentContext {name: $dst})
SET d += $props
WITH d
MATCH (s:IntentContext {name: $src})
DETACH DELETE s
"""

# ----------------------------------------------------------------------------- reads
# $persona may be null → aggregate over all personas.
READ_CONTEXTS = """
MATCH (c:IntentContext)
WHERE $persona IS NULL OR c.name = $persona
RETURN properties(c) AS c
"""

# n = the persona's count (all-persona weight when $persona is null), total = global weight
READ_TOP_WORDS = """
MATCH (w:Word)
WHERE $persona IS NULL OR $persona IN w.personas
WITH w, CASE WHEN $persona IS NULL THEN w.weight
             ELSE w.persona_counts[[i IN range(0, size(w.personas) - 1) WHERE w.personas[i] = $persona][0]]
        END AS n
RETURN w.key AS key, w.text AS text, n, w.weight AS total
ORDER BY n DESC, key ASC
LIMIT $limit
"""

# Bigrams are expanded from the persona's top words ($seed = their keys, index lookups on
# Word.key) instead of scanning every FOLLOWED_BY relationship: frequent bigrams start with
# frequent words.
READ_TOP_BIGRAMS = """
MATCH (a:Word) WHERE a.key IN $seed
MATCH (a)-[r:FOLLOWED_BY]->(b:Word)
WHERE $persona IS NULL OR $persona IN r.personas
WITH a, b, CASE WHEN $persona IS NULL THEN r.weight
             ELSE r.persona_weights[[i IN range(0, size(r.personas) - 1) WHERE r.personas[i] = $persona][0]]
        END AS n
RETURN a.key AS ak, a.text AS a, b.key AS bk, b.text AS b, n
ORDER BY n DESC, ak ASC, bk ASC
LIMIT $limit
"""

READ_STRUCTURES = """
MATCH (s:SentenceStructure)-[r:USED_IN]->(c:IntentContext)
WHERE $persona IS NULL OR c.name = $persona
RETURN s.signature AS sig, sum(r.weight) AS n
ORDER BY n DESC, sig ASC
LIMIT $limit
"""

READ_PUNCTUATION = """
MATCH (p:PunctuationMark)-[r:USED_IN]->(c:IntentContext)
WHERE $persona IS NULL OR c.name = $persona
RETURN p.symbol AS sym, sum(r.weight) AS n
"""

# $words = word keys
READ_FOLLOWED_AMONG = """
MATCH (a:Word)-[r:FOLLOWED_BY]->(b:Word)
WHERE a.key IN $words AND b.key IN $words AND ($persona IS NULL OR $persona IN r.personas)
RETURN a.key AS a, b.key AS b,
       CASE WHEN $persona IS NULL THEN r.weight
            ELSE r.persona_weights[[i IN range(0, size(r.personas) - 1) WHERE r.personas[i] = $persona][0]]
       END AS n
"""

READ_PART_OF_AMONG = """
MATCH (w:Word)-[r:PART_OF_STRUCTURE]->(s:SentenceStructure)
WHERE w.key IN $words AND s.signature IN $sigs
RETURN w.key AS w, s.signature AS sig, r.weight AS n
"""

READ_USED_IN_AMONG = """
MATCH (x)-[r:USED_IN]->(c:IntentContext)
WHERE c.name IN $ctxs AND ((x:SentenceStructure AND x.signature IN $sigs)
                           OR (x:PunctuationMark AND x.symbol IN $syms))
RETURN CASE WHEN x:SentenceStructure THEN 's' ELSE 'p' END AS kind,
       coalesce(x.signature, x.symbol) AS key, c.name AS ctx, r.weight AS n
"""

# Size of the graph (scalability tests / diagnostics). $keys = word keys to report degrees for.
GRAPH_SIZE = """
RETURN COUNT { (:Word) } AS words,
       COUNT { ()-[:FOLLOWED_BY]->() } AS followed_by,
       COUNT { ()-[:PART_OF_STRUCTURE]->() } AS part_of
"""

WORD_DEGREES = """
UNWIND $keys AS k
MATCH (w:Word {key: k})
RETURN k AS key, w.weight AS weight,
       COUNT { (w)-[:FOLLOWED_BY]->() } AS out, COUNT { ()-[:FOLLOWED_BY]->(w) } AS inn
"""

STATS = """
OPTIONAL MATCH (c:IntentContext)
RETURN COUNT { (:Word) } AS words,
       COUNT { (:SentenceStructure) } AS structures,
       coalesce(sum(c.sample_count), 0) AS samples,
       coalesce(sum(c.len_sum), 0) AS len_sum,
       coalesce(sum(c.sentence_count), 0) AS sentences
"""

PING = "RETURN 1 AS ok"

# ----------------------------------------------------------------------------- encryption migration
# Batched, resumable and idempotent: each batch is its own write transaction and only touches
# nodes that are not migrated yet (Word without ``key``; IntentContext without ``opener_texts``).
# The marker is written last, so a crash mid-way simply resumes at the next start.
META_ENC_VERSION = "MATCH (m:PolytoneMeta {name: 'schema'}) RETURN m.enc_version AS v"
SET_META_ENC_VERSION = "MERGE (m:PolytoneMeta {name: 'schema'}) SET m.enc_version = $v"
MIGRATE_WORDS_BATCH = """
MATCH (w:Word) WHERE w.key IS NULL AND w.text IS NOT NULL
RETURN elementId(w) AS id, w.text AS text
LIMIT $limit
"""
MIGRATE_WORDS_APPLY = """
UNWIND $rows AS row
MATCH (w:Word) WHERE elementId(w) = row.id AND w.key IS NULL
SET w.key = row.key, w.text = row.enc
RETURN count(w) AS n
"""
MIGRATE_CONTEXTS_BATCH = """
MATCH (c:IntentContext) WHERE c.opener_texts IS NULL
RETURN elementId(c) AS id, c.opener_keys AS openers, c.opener_phrase_keys AS phrases,
       c.transition_keys AS transitions
LIMIT $limit
"""
MIGRATE_CONTEXTS_APPLY = """
UNWIND $rows AS row
MATCH (c:IntentContext) WHERE elementId(c) = row.id AND c.opener_texts IS NULL
SET c.opener_keys = row.opener_keys, c.opener_texts = row.opener_texts,
    c.opener_phrase_keys = row.phrase_keys, c.opener_phrase_texts = row.phrase_texts,
    c.transition_keys = row.transition_keys, c.transition_texts = row.transition_texts
RETURN count(c) AS n
"""
DROP_LEGACY_WORD_CONSTRAINT = "DROP CONSTRAINT word_text IF EXISTS"

# ----------------------------------------------------------------------------- recency decay
# graph/decay.py. The epoch lives in the encryption marker node. Rebase protocol (crash-safe,
# never double-applied): (1) BEGIN records the target t0 / half-life, the factor and a new
# rebase_epoch in PolytoneMeta (one transaction; a pending rebase is resumed, never restarted);
# (2) every REBASE query multiplies the decayed properties of the nodes / relationships whose
# decay_epoch is still below that epoch by the factor AND stamps the epoch, in batches of
# __BATCH__ rows that each commit atomically, so a crash leaves every item either fully rebased
# and stamped or untouched, and rerunning only picks the untouched ones; (3) FINISH moves
# decay_t0 to the target. Until then reads keep using the old t0 (the store blocks its own
# reads and writes while it rebases). REBASE / PRUNE use ``CALL (x) {…} IN TRANSACTIONS``
# (Neo4j ≥ 5.23; run as auto-commit queries), one scan per query however large the graph.
# properties(m), not m.<prop>: absent keys must not log "property key does not exist" warnings
META_DECAY = "MATCH (m:PolytoneMeta {name: 'schema'}) RETURN properties(m) AS m"
INIT_DECAY = """
MERGE (m:PolytoneMeta {name: 'schema'})
WITH m, properties(m) AS p
SET m.decay_t0 = coalesce(p.decay_t0, $now), m.decay_half_life_s = coalesce(p.decay_half_life_s, $h)
RETURN properties(m) AS m
"""
# the no-op SET locks the marker, so two processes cannot begin two different rebases
LOCK_META = """
MERGE (m:PolytoneMeta {name: 'schema'})
SET m.name = m.name
RETURN properties(m) AS m
"""
BEGIN_REBASE = """
MATCH (m:PolytoneMeta {name: 'schema'})
SET m.rebase_epoch = $epoch, m.rebase_target_t0 = $target_t0, m.rebase_factor = $factor,
    m.rebase_half_life_s = $target_h
"""
FINISH_REBASE = """
MATCH (m:PolytoneMeta {name: 'schema'}) WHERE m.rebase_epoch = $epoch AND m.rebase_target_t0 IS NOT NULL
SET m.decay_t0 = m.rebase_target_t0, m.decay_half_life_s = m.rebase_half_life_s
REMOVE m.rebase_target_t0, m.rebase_factor, m.rebase_half_life_s
RETURN properties(m) AS m
"""
# after a wipe there is nothing left to rebase: restart the epoch at $now
RESET_DECAY = """
MERGE (m:PolytoneMeta {name: 'schema'})
SET m.decay_t0 = $now, m.decay_half_life_s = $h
REMOVE m.rebase_target_t0, m.rebase_factor, m.rebase_half_life_s
"""

_REBASE_EACH = """
MATCH {pattern} WHERE coalesce(x.decay_epoch, 0) < $epoch
CALL (x) {{
  SET {sets}, x.decay_epoch = $epoch
}} IN TRANSACTIONS OF __BATCH__ ROWS
"""


def _rebase(pattern: str, *props: str, lists: tuple[str, ...] = ()) -> str:
    sets = [f"x.{p} = coalesce(x.{p}, 0) * $factor" for p in props]
    sets += [f"x.{p} = [v IN coalesce(x.{p}, []) | v * $factor]" for p in lists]
    return _REBASE_EACH.format(pattern=pattern, sets=",\n      ".join(sets))


_CTX_REBASE = _rebase(
    "(x:IntentContext)", "sentence_count", "lower_starts", "upper_starts", "len_sum", "contractions",
    "expansions", lists=("len_hist", "opener_counts", "opener_phrase_counts", "transition_counts"),
).replace(  # sample_weight first: legacy contexts have none (= sample_count, written at t0)
    "SET ", "SET x.sample_weight = coalesce(x.sample_weight, x.sample_count, 0) * $factor,\n      ", 1
)
REBASE = [  # (what, query) — run in this order, each its own auto-commit query
    ("words", _rebase("(x:Word)", "weight", lists=("persona_counts",))),
    ("followed_by", _rebase("()-[x:FOLLOWED_BY]->()", "weight", lists=("persona_weights",))),
    ("part_of", _rebase("()-[x:PART_OF_STRUCTURE]->()", "weight")),
    ("used_in", _rebase("()-[x:USED_IN]->()", "weight")),
    ("structures", _rebase("(x:SentenceStructure)", "weight")),
    ("punctuation", _rebase("(x:PunctuationMark)", "weight")),
    ("contexts", _CTX_REBASE),
]

# Prune (effective weight = weight / s(now) < PRUNE_BELOW, i.e. weight < $below): faded
# FOLLOWED_BY / PART_OF_STRUCTURE edges, then words with a faded weight and no edge left.
_PRUNE_EACH = """
MATCH {pattern} WHERE {where}
CALL (x) {{
  DELETE x
}} IN TRANSACTIONS OF __BATCH__ ROWS
RETURN count(*) AS n
"""
PRUNE = [  # (count key, query)
    ("followed_by", _PRUNE_EACH.format(pattern="()-[x:FOLLOWED_BY]->()", where="x.weight < $below")),
    ("part_of", _PRUNE_EACH.format(pattern="()-[x:PART_OF_STRUCTURE]->()", where="x.weight < $below")),
    ("words", _PRUNE_EACH.format(pattern="(x:Word)", where="x.weight < $below AND NOT (x)--()")),
]

# ----------------------------------------------------------------------------- wipe
WIPE_BATCH = """
MATCH (n) WHERE n:Word OR n:SentenceStructure OR n:PunctuationMark OR n:IntentContext
WITH n LIMIT $limit
DETACH DELETE n
RETURN count(*) AS n
"""
