"""Cypher templates for Neo4jGraphStore. All parameterized; writes are batched UNWINDs.

Schema (CONTRACTS.md):
  (:Word {text, weight, personas, persona_counts})
  (:SentenceStructure {signature, weight})
  (:PunctuationMark {symbol, weight})
  (:IntentContext {name, sample_count, sentence_count, lower_starts, upper_starts, len_sum,
                   len_hist, contractions, expansions, opener_keys, opener_counts,
                   opener_phrase_keys, opener_phrase_counts, transition_keys, transition_counts})
  (:Word)-[:FOLLOWED_BY {weight, personas, persona_weights}]->(:Word)
  (:Word)-[:PART_OF_STRUCTURE {weight}]->(:SentenceStructure)
  (:SentenceStructure)-[:USED_IN {weight}]->(:IntentContext)
  (:PunctuationMark)-[:USED_IN {weight}]->(:IntentContext)

IntentContext.name is the persona. Per-persona weights on Word / FOLLOWED_BY are stored as
two parallel lists (``personas[i]`` ↔ ``persona_counts[i]`` / ``persona_weights[i]``) because
Neo4j properties cannot hold maps and we avoid APOC.
"""

SCHEMA = [
    "CREATE CONSTRAINT word_text IF NOT EXISTS FOR (w:Word) REQUIRE w.text IS UNIQUE",
    ("CREATE CONSTRAINT structure_signature IF NOT EXISTS "
    "FOR (s:SentenceStructure) REQUIRE s.signature IS UNIQUE"),
    "CREATE CONSTRAINT punct_symbol IF NOT EXISTS FOR (p:PunctuationMark) REQUIRE p.symbol IS UNIQUE",
    "CREATE CONSTRAINT context_name IF NOT EXISTS FOR (c:IntentContext) REQUIRE c.name IS UNIQUE",
]

# ----------------------------------------------------------------------------- writes
MERGE_CONTEXT = """
MERGE (c:IntentContext {name: $persona})
ON CREATE SET c.sample_count = 0, c.sentence_count = 0, c.lower_starts = 0, c.upper_starts = 0,
              c.len_sum = 0, c.len_hist = [], c.contractions = 0, c.expansions = 0,
              c.opener_keys = [], c.opener_counts = [], c.transition_keys = [], c.transition_counts = [],
              c.opener_phrase_keys = [], c.opener_phrase_counts = []
SET c.sample_count = c.sample_count + 1,
    c.sentence_count = c.sentence_count + $d.sentence_count,
    c.lower_starts = c.lower_starts + $d.lower_starts,
    c.upper_starts = c.upper_starts + $d.upper_starts,
    c.len_sum = c.len_sum + $d.len_sum,
    c.contractions = c.contractions + $d.contractions,
    c.expansions = c.expansions + $d.expansions,
    c.len_hist = [i IN range(0, size($d.len_hist) - 1) | coalesce(c.len_hist[i], 0) + $d.len_hist[i]],
    c.opener_phrase_keys = coalesce(c.opener_phrase_keys, []),
    c.opener_phrase_counts = coalesce(c.opener_phrase_counts, [])
WITH c
SET c.opener_keys = c.opener_keys + [k IN keys($d.openers) WHERE NOT k IN c.opener_keys],
    c.transition_keys = c.transition_keys + [k IN keys($d.transitions) WHERE NOT k IN c.transition_keys],
    c.opener_phrase_keys = c.opener_phrase_keys
                           + [k IN keys($d.opener_phrases) WHERE NOT k IN c.opener_phrase_keys]
WITH c
SET c.opener_counts = [i IN range(0, size(c.opener_keys) - 1) |
                         coalesce(c.opener_counts[i], 0) + coalesce($d.openers[c.opener_keys[i]], 0)],
    c.transition_counts = [i IN range(0, size(c.transition_keys) - 1) |
                         coalesce(c.transition_counts[i], 0) + coalesce($d.transitions[c.transition_keys[i]], 0)],
    c.opener_phrase_counts = [i IN range(0, size(c.opener_phrase_keys) - 1) |
                         coalesce(c.opener_phrase_counts[i], 0)
                         + coalesce($d.opener_phrases[c.opener_phrase_keys[i]], 0)]
"""

MERGE_WORDS = """
UNWIND $rows AS row
MERGE (w:Word {text: row.text})
ON CREATE SET w.weight = 0, w.personas = [], w.persona_counts = []
SET w.weight = w.weight + row.n
WITH w, row
SET w.personas = CASE WHEN $persona IN w.personas THEN w.personas ELSE w.personas + $persona END
WITH w, row
SET w.persona_counts = [i IN range(0, size(w.personas) - 1) |
    coalesce(w.persona_counts[i], 0) + CASE WHEN w.personas[i] = $persona THEN row.n ELSE 0 END]
"""

MERGE_BIGRAMS = """
UNWIND $rows AS row
MATCH (a:Word {text: row.a})
MATCH (b:Word {text: row.b})
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
MATCH (w:Word {text: row.w})
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
RETURN w.text AS text, n, w.weight AS total
ORDER BY n DESC, text ASC
LIMIT $limit
"""

# Bigrams are expanded from the persona's top words ($seed, index lookups on Word.text) instead
# of scanning every FOLLOWED_BY relationship: frequent bigrams start with frequent words.
READ_TOP_BIGRAMS = """
MATCH (a:Word) WHERE a.text IN $seed
MATCH (a)-[r:FOLLOWED_BY]->(b:Word)
WHERE $persona IS NULL OR $persona IN r.personas
WITH a, b, CASE WHEN $persona IS NULL THEN r.weight
             ELSE r.persona_weights[[i IN range(0, size(r.personas) - 1) WHERE r.personas[i] = $persona][0]]
        END AS n
RETURN a.text AS a, b.text AS b, n
ORDER BY n DESC, a ASC, b ASC
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

READ_FOLLOWED_AMONG = """
MATCH (a:Word)-[r:FOLLOWED_BY]->(b:Word)
WHERE a.text IN $words AND b.text IN $words AND ($persona IS NULL OR $persona IN r.personas)
RETURN a.text AS a, b.text AS b,
       CASE WHEN $persona IS NULL THEN r.weight
            ELSE r.persona_weights[[i IN range(0, size(r.personas) - 1) WHERE r.personas[i] = $persona][0]]
       END AS n
"""

READ_PART_OF_AMONG = """
MATCH (w:Word)-[r:PART_OF_STRUCTURE]->(s:SentenceStructure)
WHERE w.text IN $words AND s.signature IN $sigs
RETURN w.text AS w, s.signature AS sig, r.weight AS n
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
