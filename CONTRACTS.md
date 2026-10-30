# Polytone — Shared Contracts

Every component builds against this file. Change it only with the orchestrator's agreement.

## Process layout

```
┌──────────── Tauri app (Rust, src-tauri) ────────────┐        ┌──── Python backend (backend/) ────┐
│ tray / menu bar  ·  global hotkey  ·  capture/inject │ HTTP → │ FastAPI on 127.0.0.1:8765          │
│ React UI (src/) talks to the backend over HTTP too   │        │ LangGraph generation + ingestion   │
└──────────────────────────────────────────────────────┘        │ Neo4j (docker) · SQLite (data/)    │
                                                                 │ MCP servers (stdio, separate procs)│
                                                                 └────────────────────────────────────┘
```

- Backend entrypoint: `uv run polytone-api` (= `backend.daemon.server:main`), port `POLYTONE_API_PORT` (default 8765).
- Config: `backend/config.py` → `get_settings()` reads `.env` (see `.env.example`). Never hardcode secrets.
- LLM: `POLYTONE_LLM=mock` OR no `ANTHROPIC_API_KEY` → deterministic mock LLM. Real model `claude-sonnet-5-5`.
- Embeddings: `POLYTONE_EMBEDDINGS=none` → feature-only scoring. Tests always use `none`.
- Python 3.11, managed with `uv` (`pyproject.toml` at repo root, package `backend`).

## Hotkey loop (Rust ↔ backend)

1. Hotkey `CmdOrCtrl+Shift+P` (configurable) fires in Rust.
2. Rust capture: save clipboard → write sentinel → send Cmd+C → if clipboard ≠ sentinel use it (selection);
   else send Cmd+A, Cmd+C → use field text; if still empty/sentinel → fall back to the ORIGINAL clipboard;
   if that is empty too → show "Type or select your rough intent first" notification and stop.
3. Rust → `POST /generate`.
4. Rust inject: put draft on clipboard, send Cmd+V (field text is still selected, so it is replaced),
   then restore the original clipboard after ~300ms. Fallback `inject_mode=type` types chars via enigo.
5. End-to-end budget: 5s. Rust HTTP timeout 8s → notification "Polytone timed out".

## HTTP API (JSON, all on 127.0.0.1:8765, CORS allows tauri://localhost + http://localhost:1420)

| Method | Path | Request | Response |
|---|---|---|---|
| GET | `/health` | – | `{"status":"ok","neo4j":bool,"llm":"anthropic"\|"mock","embeddings":"qwen"\|"none","graph_store":"neo4j"\|"memory"}` |
| POST | `/generate` | `{"intent":str,"persona":str\|null,"app_context":str\|null}` | `{"draft_id":str,"text":str,"persona":str,"latency_ms":int,"constraints":Constraints}` |
| POST | `/ingest/manual` | `{"text":str,"persona":str\|null}` | `IngestResult` |
| GET | `/ingest/sources` | – | `[{"name":"manual"\|"gmail"\|"wispr"\|"watcher","enabled":bool,"status":str,"last_run":iso\|null,"items_ingested":int}]` |
| POST | `/ingest/sources/{name}/run` | – | `IngestResult` |
| GET | `/personas` | – | `[{"name":str,"description":str}]` (defaults: Boss, Friend, Investor, Default) |
| GET/PUT | `/config` | `{"active_persona":str,"hotkey":str,"inject_mode":"paste"\|"type"}` | same |
| GET | `/drafts?limit=50` | – | `[{"draft_id","created_at","persona","intent","text","latency_ms","score":ScoreBreakdown\|null}]` |
| GET | `/scores/summary` | – | `{"mean":float\|null,"count":int,"trend":[{"t":iso,"score":float}]}` |
| GET | `/graph?limit=60&persona=` | – | `{"nodes":[{"id":str,"label":"Word"\|"SentenceStructure"\|"PunctuationMark"\|"IntentContext","name":str,"weight":float}],"edges":[{"source":str,"target":str,"type":"FOLLOWED_BY"\|"PART_OF_STRUCTURE"\|"USED_IN","weight":float}]}` |
| GET | `/stats` | – | `{"words":int,"structures":int,"samples":int,"avg_sentence_len":float\|null}` |

`Constraints` = `{"avg_sentence_len":float,"sentence_len_p90":float,"top_openers":[str],"top_transitions":[str],"top_bigrams":[[str,str,float]],"punctuation_rates":{str:float},"structures":[{"signature":str,"weight":float}],"lexicon":[str],"casing":"lower"|"sentence"|"mixed","contractions_rate":float,"sample_count":int}`

`IngestResult` = `{"accepted":int,"rejected":int,"reasons":[str],"sentences":int,"words_merged":int}`

`ScoreBreakdown` = `{"score":float /*0-100, 100 = identical style*/,"distance":float /*0-1*/,"components":{"sentence_length":float,"punctuation":float,"lexical":float,"function_words":float,"embedding":float|null}}`

## Python module boundaries (backend/)

- `config.py` — `Settings`, `get_settings()`.
- `storage/db.py` — SQLite (`data/polytone.sqlite3`): tables `config`, `drafts`, `samples` (ingested human text + provenance + sha256), `ingest_runs`. Functions documented in that file.
- `graph/store.py` — `GraphStore` protocol + `Neo4jGraphStore` + `InMemoryGraphStore` (tests). Cypher templates live in `graph/queries.py`.
- `ingestion/extract.py` — `extract(text) -> Extraction` (pure python, deterministic, no network). Triplets: Word FOLLOWED_BY Word (weight), Word PART_OF_STRUCTURE SentenceStructure, SentenceStructure USED_IN IntentContext, PunctuationMark nodes.
- `ingestion/firewall.py` — `HumanText` provenance gate. Rejects: empty, text whose sha256 or near-duplicate (shingle Jaccard ≥ 0.8) matches any stored draft, inbound mail, text flagged `generated=True`. **Generated drafts never reach the graph.**
- `langgraph/ingest_graph.py` — LangGraph: `validate → firewall → extract → write_graph → record`.
- `langgraph/generate_graph.py` — LangGraph: `capture → retrieve_constraints → build_prompt → generate → postprocess → record → score`.
- `llm/client.py` — `LLM.complete(system, user) -> str`; `AnthropicLLM`, `MockLLM`.
- `evaluation/style_distance.py` — `score(text, baseline_texts) -> ScoreBreakdown`.
- `mcp_servers/` — FastMCP stdio servers: `graph_server.py` (tool `execute_read`, `get_constraints`), `gmail_outbound.py` (only `in:sent from:me`; inbound blocked), `wispr.py` (WebSocket client; falls back to local Wispr history), `file_watcher.py` (.txt, .mbox [only messages From: the user], SQLite dumps).
- `daemon/server.py` — FastAPI app wiring everything (`create_app(runtime)`).
- `runtime.py` — `Runtime(settings, db, store, llm, scorer)`; `build_runtime(...)`, `get_runtime()`, `set_runtime()`.
- `ingestion/types.py` — `Extraction` dataclass (producer: `extract`, consumer: `GraphStore.write_extraction`).
- `ingestion/sources.py` — source registry. Each `mcp_servers` source module may expose
  `collect(settings) -> Iterable[{"text", "provenance"?, "persona"?} | str]`, optional `is_enabled(settings)`,
  `status(settings)`; items go through `run_ingest_batch` (full firewall). Modules: gmail→`gmail_outbound`,
  wispr→`wispr`, watcher→`file_watcher`.

## Neo4j schema

Nodes: `(:Word {text})`, `(:SentenceStructure {signature})`, `(:PunctuationMark {symbol})`, `(:IntentContext {name})`.
Rels: `(:Word)-[:FOLLOWED_BY {weight, persona_weights}]->(:Word)`, `(:Word)-[:PART_OF_STRUCTURE {weight}]->(:SentenceStructure)`, `(:SentenceStructure)-[:USED_IN {weight}]->(:IntentContext)`, `(:PunctuationMark)-[:USED_IN {weight}]->(:IntentContext)`.
Constraints: unique `Word.text`, `SentenceStructure.signature`, `PunctuationMark.symbol`, `IntentContext.name`.
Writes are always `MERGE ... ON CREATE SET weight=0 SET weight = weight + $n`.
Neo4j: `bolt://localhost:7687`, user `neo4j`, password `polytone-local`.

Implementation notes (backend core, additive — see `graph/queries.py`):
- `IntentContext.name` is the persona. It also carries the persona's aggregate style counters
  (`sample_count, sentence_count, lower_starts, upper_starts, len_sum, len_hist, contractions,
  expansions, opener_keys/opener_counts, transition_keys/transition_counts`).
- Neo4j properties cannot be maps, so `persona_weights` is a list parallel to `personas`
  (`r.personas[i]` ↔ `r.persona_weights[i]`); `Word` likewise has `weight, personas, persona_counts`.
- `get_constraints(persona)` falls back to all-persona data when that persona has no samples yet.
