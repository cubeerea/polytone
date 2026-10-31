"""Cypher templates for Neo4jGraphStore. All parameterized; writes are batched UNWINDs.

Schema (CONTRACTS.md):
  (:Word {key, text, weight, personas, persona_counts})
  (:SentenceStructure {signature, weight})
  (:PunctuationMark {symbol, weight})
  (:IntentContext {name, sample_count, sentence_count, lower_starts, upper_starts, len_sum,
                   len_hist, contractions, expansions,
                   opener_keys, opener_texts, opener_counts,
                   opener_phrase_keys, opener_phrase_texts, opener_phrase_counts,
                   transition_keys, transition_texts, transition_counts})
  (:Word)-[:FOLLOWED_BY {weight, personas, persona_weights}]->(:Word)
  (:Word)-[:PART_OF_STRUCTURE {weight}]->(:SentenceStructure)
  (:SentenceStructure)-[:USED_IN {weight}]->(:IntentContext)
  (:PunctuationMark)-[:USED_IN {weight}]->(:IntentContext)
  (:PolytoneMeta {name: "schema", enc_version})   -- encryption migration marker

Encryption at rest: the user's words never reach Neo4j in plaintext. ``Word.key`` is the blind
index (``idx1:``, unique, what MERGE / MATCH use) and ``Word.text`` the ``enc1:`` ciphertext
(display value, decrypted by the store). IntentContext opener / opener-phrase / transition lists
hold blind indexes in ``*_keys`` (merged by key) and the ciphertexts in the parallel ``*_texts``.
Signatures, punctuation symbols and persona names carry no words and stay plain.
Read ordering ties break on ``key`` (both stores do the same, so results match).

IntentContext.name is the persona. Per-persona weights on Word / FOLLOWED_BY are stored as
two parallel lists (``personas[i]`` ↔ ``persona_counts[i]`` / ``persona_weights[i]``) because
Neo4j properties cannot hold maps and we avoid APOC.
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
ON CREATE SET c.sample_count = 0, c.sentence_count = 0, c.lower_starts = 0, c.upper_starts = 0,
              c.len_sum = 0, c.len_hist = [], c.contractions = 0, c.expansions = 0,
              c.opener_keys = [], c.opener_texts = [], c.opener_counts = [],
              c.transition_keys = [], c.transition_texts = [], c.transition_counts = [],
              c.opener_phrase_keys = [], c.opener_phrase_texts = [], c.opener_phrase_counts = []
SET c.sample_count = c.sample_count + 1,
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

# ----------------------------------------------------------------------------- wipe
WIPE_BATCH = """
MATCH (n) WHERE n:Word OR n:SentenceStructure OR n:PunctuationMark OR n:IntentContext
WITH n LIMIT $limit
DETACH DELETE n
RETURN count(*) AS n
"""
