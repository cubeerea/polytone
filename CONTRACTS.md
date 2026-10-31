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
- LLM: `POLYTONE_LLM=mock` OR no `ANTHROPIC_API_KEY` → deterministic mock LLM. Real model `claude-sonnet-5-5`,
  `output_config.effort` = `POLYTONE_EFFORT` (default `low`: little/no thinking → fast drafts), stable system-prompt
  part sent with `cache_control` (prompt caching), one 6.5s attempt, no retries.
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
6. `/generate` errors → Rust shows a notification and injects NOTHING (field text untouched):
   `502 {"error":"llm_failed","detail":str}` (LLM error, empty completion, or `POLYTONE_LLM=anthropic`
   without a key) · `504 {"error":"llm_timeout","detail":str}` (LLM timed out) · `422` (empty intent).
   The backend never substitutes mock text for a failed real LLM call; the mock LLM is used only when
   `settings.llm_backend == "mock"`. Failed generations record no draft.

## HTTP API (JSON, all on 127.0.0.1:8765, CORS allows tauri://localhost + http://localhost:1420)

| Method | Path | Request | Response |
|---|---|---|---|
| GET | `/health` | – | `{"status":"ok","neo4j":bool,"llm":"anthropic"\|"mock","embeddings":"qwen"\|"none","graph_store":"neo4j"\|"memory","llm_ready":bool,"wispr_live":bool}` (`llm` = configured backend; `llm_ready` false → `/generate` will 502; additive `wispr_live` = the live Wispr Flow watcher is running). Answers as soon as uvicorn is up; graph compile, constraint priming, the drop-folder watcher, the live Wispr watcher and the embedding model warm in a background thread afterwards. |
| POST | `/generate` | `{"intent":str,"persona":str\|null,"app_context":str\|null}` | `{"draft_id":str,"text":str,"persona":str,"latency_ms":int,"constraints":Constraints}`; errors `502/504 {"error":"llm_failed"\|"llm_timeout","detail":str}` |
| POST | `/ingest/manual` | `{"text":str,"persona":str\|null}` | `IngestResult` |
| POST | `/ingest/typed` | `{"text":str,"app":str /*bundle id*/,"app_name":str\|null,"after_inject":bool}` | `IngestResult` (see "Typing capture") |
| GET | `/ingest/sources` | – | `[{"name":"manual"\|"gmail"\|"wispr"\|"watcher"\|"typed","enabled":bool,"status":str,"last_run":iso\|null,"items_ingested":int}]` (`typed`: `enabled` = `capture_enabled`, `status` `"listening"`\|`"disabled"`; `wispr` while live: `"live (watching flow.sqlite; last dictation picked up <iso> (<app>); <n> ingested live)"`, else `"ready (local history: flow.sqlite)"` / `"needs credentials: …"`) |
| POST | `/ingest/sources/{name}/run` | – | `IngestResult`; `typed` → `400 {"detail":"typed is push-only"}`; unknown → 404 |
| GET | `/personas` | – | `[{"name":str,"description":str}]` (defaults: Boss, Friend, Investor, Default) |
| GET/PUT | `/config` | `{"active_persona":str,"hotkey":str,"inject_mode":"paste"\|"type","capture_enabled":bool,"capture_apps":[str]}` (PUT: every field optional, omitted = unchanged) | same (full config) |
| GET | `/drafts?limit=50` | – | `[{"draft_id","created_at","persona","intent","text","latency_ms","score":ScoreBreakdown\|null}]` |
| GET | `/scores/summary` | – | `{"mean":float\|null,"count":int,"trend":[{"t":iso,"score":float}]}` |
| GET | `/graph?limit=60&persona=` | – | `{"nodes":[{"id":str,"label":"Word"\|"SentenceStructure"\|"PunctuationMark"\|"IntentContext","name":str,"weight":float}],"edges":[{"source":str,"target":str,"type":"FOLLOWED_BY"\|"PART_OF_STRUCTURE"\|"USED_IN","weight":float}]}` |
| GET | `/stats` | – | `{"words":int,"structures":int,"samples":int,"avg_sentence_len":float\|null}` |

`Constraints` = `{"avg_sentence_len":float,"sentence_len_p90":float,"top_openers":[str],"top_transitions":[str],"top_bigrams":[[str,str,float]],"punctuation_rates":{str:float},"structures":[{"signature":str,"weight":float}],"lexicon":[str],"casing":"lower"|"sentence"|"mixed","contractions_rate":float,"sample_count":int}`

Constraints semantics (`graph/constraints.py`): persona stats are blended with all-persona stats,
`alpha = n_p/(n_p+3)` (0 samples → pure global and `sample_count` = global count; else `sample_count` = persona's).
Scalars and cadence lists (openers, transitions, structures, punctuation) blend; once a persona has ≥3 samples,
marks/items it never used are dropped. `lexicon`/`top_bigrams` are persona-only (lexicon = distinctive,
non-generic words). `structures[].weight` = blended share of sentences. `top_openers` may hold 2–3 word
phrases ("quick update on"). `punctuation_rates` keys: `, . ; : ( " ! ? !! — … emoji` (`!`/`?` per run).
Signature format `LEN|<n>c|<skeleton>|<opener>|<end>`, e.g. `M|2c|,—|conj|.` (see `ingestion/extract.py`).

`IngestResult` = `{"accepted":int,"rejected":int,"reasons":[str],"sentences":int,"words_merged":int}`

`ScoreBreakdown` = `{"score":float /*0-100, 100 = identical style*/,"distance":float /*0-1*/,"components":{"sentence_length":float,"punctuation":float,"lexical":float,"function_words":float,"embedding":float|null},"confidence":"low"|"medium"|"high" /*additive: trust in the number given baseline size + draft length*/}`

## Typing capture (source `typed`; opt-in, default OFF, local only)

Learns from messages the user types and **sends** in allowlisted native apps.

- Config: `capture_enabled` (default `false`), `capture_apps` (bundle ids; default
  `com.tinyspeck.slackmacgap, com.apple.MobileSMS, com.apple.mail, com.apple.Notes, com.hnc.Discord,
  net.whatsapp.WhatsApp, ru.keepcoder.Telegram, com.google.Chrome, com.apple.Safari,
  company.thebrowser.Browser, notion.id`). Persisted in SQLite `config` (bool/list stored as JSON).
- Rust (`src-tauri/src/typing_capture/`): listen-only `CGEventTap` (keyDown) on its own thread; the
  callback only forwards keys / re-enables a timed-out tap. A worker skips everything unless capture is
  on, the hotkey isn't paused (`AppState.paused`), `IsSecureEventInputEnabled()` is false and the
  frontmost app is allowlisted. Edit keys → debounced (300ms) read of the focused element's `AXValue`
  (roles AXTextArea/AXTextField/AXComboBox; never `AXSecureTextField` or `AXSearchField`), cached per app.
  Send keys: Return without Shift/Opt/Ctrl/Cmd (except Mail, Notes, Notion where Return is a newline),
  Cmd+Return (any app), Cmd+Shift+D (Mail). The send is **confirmed** by re-reading the field after
  250ms / 900ms: it must clear, change, or lose focus, else the Return was a newline and nothing is sent.
  Never captures on app switch. Drops < 3 words, caps at 5000 chars, never resends identical text per app.
  A send in the app Polytone last injected into, within 10 minutes of that inject → `after_inject=true`.
  `POST /ingest/typed` fire-and-forget; failures go to `last_error`, never per-item notifications.
- Tauri commands: `set_capture(enabled: bool, apps: string[]) -> CaptureStatus` (applies at once and
  retries a failed start; the UI also PUTs `/config`), `get_capture_status() -> CaptureStatus`,
  `list_running_apps() -> [{"bundle_id":str,"name":str}]` (regular GUI apps).
  `CaptureStatus` = `{"enabled":bool,"running":bool,"apps":[str],"captured_this_session":u32,
  "last_captured_at":iso|null,"last_error":str|null}`. Missing Input Monitoring/Accessibility → the tap
  can't be created: `running=false`, `last_error` says which permission. Startup reads `/config`
  (`sync_from_backend_config`).
- Backend `POST /ingest/typed`: `after_inject=true` → never learned; stored in SQLite `draft_edits`
  (`id, created_at, app, text, nearest_draft_id` = newest draft of the last 10 min or null) and returns
  `{"accepted":0,"rejected":1,"reasons":["after_polytone_inject: stored for edit learning"],...}`.
  Otherwise, if `capture_enabled` is false → rejected (`"typing capture is disabled ..."`); else the full
  ingest graph with source `typed`, provenance `{"typed":true,"app":...,"app_name"?}`, persona `Default`
  (the global pool — not the active persona). Text capped at 5000 chars.
- Wispr Flow interplay: before learning, spans Wispr inserted into the message are removed
  (`wispr_live.check_typed`): any recent Wispr insertion (History rows of the last 30 min — by
  `timestamp` + `duration` — with the same `app` bundle id; variants `pastedText`,
  `serverFinalizedText`, `formattedText`, `asrText`) whose tokens occur verbatim in the text (≥ 2
  tokens, or the whole text), or whose 3-word shingles cover ≥ 80% of it / of the text, is cut out
  (with ≤ 2-token gaps between cut spans). The remainder must pass `firewall.check` on its own, else
  `{"accepted":0,"rejected":1,"reasons":["wispr_inserted: the message is Wispr Flow dictation output (<firewall reason>)"]}`;
  if it passes it is learned with provenance `wispr_removed: {"rowids":[int],"tokens":int}`. The
  index is fed by the live watcher and refreshed from the DB on demand whenever the DB changed
  (looks back ≤ 50 rows); with no match it re-polls for `WISPR_TYPED_GRACE_MS` (default 1500) in
  case the dictation is still being committed. No Wispr install → no check, no wait.

## Wispr Flow (source `wispr`; local, read-only)

- DB: `~/Library/Application Support/Wispr Flow/flow.sqlite` (WAL). Never opened in place: the DB + `-wal`
  are APFS-cloned to a temp dir (no `-shm`: SQLite rebuilds the WAL index; retried if the file signature
  changed mid-copy) and the copy is opened `mode=ro`. Only named columns are selected (`rowid`,
  `transcriptEntityId`, `timestamp`, `duration`, `app`, `url`, `asrText`, `status`, `isArchived`,
  `pastedText`, `serverFinalizedText`, `formattedText`) — never `*`, never the blob columns.
- Learned: `asrText` (raw ASR) only, unchanged; rows with status `dismissed` (or error-like), blank
  `asrText`, or filler-only text ("um uh") are skipped; **archived rows are learned**. Item = `{"text": asrText,
  "persona": "Default", "provenance": {"source":"wispr","raw_transcript":true,"kind":"spoken",
  "origin":"wispr_local_history","field":"asrText","rowid","transcript_id","timestamp","app","url"?,
  "duration"?,"archived"?}}`. Formatted/pasted text is never ingested or stored in provenance.
- Cursor: SQLite config `wispr.history_rowid` (`rowid > cursor`, insert order; `timestamp` is the
  dictation start and not monotonic). Migrated once from `wispr.history_last_ts` (kept updated) as
  `MAX(rowid) WHERE timestamp <= old`. Shared by the live watcher and `POST /ingest/sources/wispr/run`
  (one read at a time). Rows with no text and no status that are < 15 min old are re-read later.
- Live: `POLYTONE_WISPR_LIVE` (default `1`; only when the DB exists and `POLYTONE_WISPR_LOCAL` ≠ `0`)
  starts `wispr_live.WisprLiveWatcher` with the server: stat DB + `-wal` every 1 s → on change debounce
  300 ms → snapshot + incremental read (≤ 200 rows per tick) → `run_ingest_batch(items, "wispr")` +
  feed the typed-capture insertion index (see Typing capture).

## Python module boundaries (backend/)

- `config.py` — `Settings`, `get_settings()`.
- `storage/db.py` — SQLite (`data/polytone.sqlite3`): tables `config`, `drafts`, `samples` (ingested human text + provenance + sha256), `ingest_runs`, `draft_edits` (post-inject sends; never learned). `Database(path)` methods documented in that file (the old module-level wrappers are gone; use `Runtime.db`). One persistent connection per thread, WAL + `synchronous=NORMAL`; `draft_watermark()`/`drafts_since(rowid)`/`samples_watermark()` let in-process caches stay exact across processes.
- `graph/store.py` — `GraphStore` protocol + `Neo4jGraphStore` + `InMemoryGraphStore` (tests). Cypher templates live in `graph/queries.py`.
- `ingestion/extract.py` — `extract(text) -> Extraction` (pure python, deterministic, no network). Triplets: Word FOLLOWED_BY Word (weight), Word PART_OF_STRUCTURE SentenceStructure, SentenceStructure USED_IN IntentContext, PunctuationMark nodes.
- `ingestion/firewall.py` — `HumanText` provenance gate; every rejection returns a reason string. Rejects: empty,
  `generated=True`/`origin="polytone"`, inbound mail, per-source provenance failures, < 3 words / < 10 chars,
  duplicate samples (sha256), and anything draft-like: sha256 match, a draft (≥4 tokens) contained in the text or the
  text contained in a draft, shingle Jaccard ≥ 0.8, or ≥ 80% of a draft's shingles inside the text.
  **Generated drafts never reach the graph.** Draft checks use an in-memory index per DB file (inverted shingle
  index, synced incrementally from `drafts` on every check — so drafts written by any process count); verdicts are
  identical to comparing against every draft. Required source provenance:
  gmail → `from_me=True` or `label`/`labels` containing `SENT` (`from_me=False` always rejects);
  watcher → `.mbox` items (`kind`/`format`="mbox" or `file`/`path` *.mbox) need `from_me=True`;
  wispr → `raw_transcript=True` (or `text_kind="raw"`); `formatted`/`edited`/`ai_edited`/`polished` = reject
  (local-history items also carry `kind="spoken"` — the spoken register, learned under `Default`, splittable later);
  typed → `typed=True` (`after_inject=True` always rejects).
- `langgraph/ingest_graph.py` — LangGraph: `validate → firewall → extract → write_graph → record`. `validate` strips
  quoted reply chains and signatures (`ingestion/text.clean_message`); the cleaned text is what is checked, learned, stored.
- `langgraph/generate_graph.py` — LangGraph: `capture → retrieve_constraints → build_prompt → generate → postprocess → record → score`.
  `retrieve_constraints` also selects 2–3 few-shot excerpts from SQLite `samples` (`langgraph/examples.py`; never drafts;
  memoized per persona/intent-length/constraints/samples-watermark, re-checked against drafts added since).
  The system prompt is built as `(stable, volatile)` parts (`prompts.build_system_parts`): stable = style rules +
  persona + content rules (the prompt-cache prefix), volatile = few-shot examples + destination-app hint.
  LLM failure raises `LLMFailedError`/`LLMTimeoutError` (→ 502/504), never a mock fallback.
- `llm/client.py` — `LLM.complete(system, user) -> str`; `AnthropicLLM`, `MockLLM`. Clients with
  `cache_system = True` (AnthropicLLM) receive `system` as the `(stable, volatile)` parts; others get one string.
  `stop_reason == "refusal"` raises `LLMRefusal` (→ 502 `llm_failed`).
- `evaluation/style_distance.py` — `score(text, baseline_texts, persona=None) -> ScoreBreakdown`; `score_draft(draft_id, runtime_or_db)` re-scores + persists. Features in `evaluation/features.py`, optional Qwen3 embedding in `evaluation/embeddings.py`, `python -m backend.evaluation.calibrate` prints sanity numbers.
- `mcp_servers/` — FastMCP stdio servers: `graph_server.py` (tool `execute_read`, `get_constraints`), `gmail_outbound.py` (only `in:sent from:me`; inbound blocked), `wispr.py` (WebSocket client; falls back to local Wispr history), `wispr_live.py` (live Wispr watcher + insertion index), `file_watcher.py` (.txt, .mbox [only messages From: the user], SQLite dumps).
- `daemon/server.py` — FastAPI app wiring everything (`create_app(runtime)`).
- `runtime.py` — `Runtime(settings, db, store, llm, scorer)`; `build_runtime(...)`, `get_runtime()`, `set_runtime()`.
- `ingestion/types.py` — `Extraction` dataclass (producer: `extract`, consumer: `GraphStore.write_extraction`).
  Additive fields: `opener_ngrams, contractions, expansions, lower_i, upper_i` (all defaulted).
- `ingestion/sources.py` — source registry. Each `mcp_servers` source module may expose
  `collect(settings) -> Iterable[{"text", "provenance"?, "persona"?} | str]`, optional `is_enabled(settings)`,
  `status(settings)`; items go through `run_ingest_batch` (full firewall). Modules: gmail→`gmail_outbound`,
  wispr→`wispr`, watcher→`file_watcher`. `typed` is push-only (`ingest_typed(rt, text, app, app_name, after_inject)`,
  called by `POST /ingest/typed`); running it is refused.

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
- `get_constraints(persona)` blends the persona with all-persona data (see Constraints semantics above). Results are
  cached per (persona, k) and the all-persona aggregate once for every persona; any write invalidates (a generation
  counter keeps a computation that raced a write out of the cache). After an accepted ingest the server re-primes
  the active persona's constraints in the background. Each Neo4j aggregate is read in one read transaction.
- `IntentContext` also stores `opener_phrase_keys/opener_phrase_counts` (2–3 word openers).

## Security

Threat model: one user on their own Mac. Protect learned text at rest (stolen disk image, backups,
other local programs), stop other local processes and web pages from reading the API, record who did
what in a log that reveals tampering, and keep everything leaving the machine on TLS 1.3. Out of scope:
an attacker running as the same user with the Polytone window unlocked, or root.

### Keys (`backend/security/keys.py`)
- 256-bit master key in the macOS Keychain (service `com.polytone.app`, account `data-key-v1`),
  created on first use. `POLYTONE_DATA_KEY` (base64, 32 bytes) overrides it (tests set it in conftest).
- `subkey(purpose)` = HKDF-SHA256(master, `polytone/<purpose>/v1`). Purposes: `enc`, `index`, `audit`.
- Only the backend reads the key. Rust never sees it.

### Encryption at rest (`backend/security/crypto.py`)
- AES-256-GCM, random 96-bit nonce, AAD = a context string (`"<table>.<column>"` / `"neo4j:<Label>.<prop>"`).
  Stored as text `enc1:<base64url(nonce||ciphertext||tag)>`. Decrypt failure raises, never returns garbage.
- Blind index for anything looked up / made unique by content: `idx1:<base64url(HMAC-SHA256(subkey("index"), context||value))[:32]>`.
  Replaces the plain `sha256` dedupe column (a plain hash of short text is guessable).
- Every column/property holding user text is encrypted: SQLite `drafts.intent/text`, `samples.text`,
  `samples.provenance` (if it carries text), `draft_edits.text`, and Neo4j word/bigram/opener/text
  properties (lookup key = blind index, display value = ciphertext). Counts, timestamps, persona names,
  config and cursors stay plain.
- Migration runs once at backend start: idempotent (already-`enc1:` values skipped), one transaction
  per table / Neo4j batch, then `VACUUM` + WAL checkpoint so plaintext pages don't linger.
- Embedding vectors (if stored) are encrypted too.

### Local API auth (backend ↔ Rust ↔ UI)
- **App token**: backend uses `POLYTONE_API_TOKEN` if set, else generates 32 random bytes (base64url).
  It always writes the token to `<data_dir>/.api_token` (mode 0600, atomic replace). Rust generates the
  token and passes it in the env when it spawns the backend; when it attaches to an already-running
  backend it reads the file. Sent as `Authorization: Bearer <token>`. Only Rust holds it. Full scope.
- **UI session** ("auto-logout"): `POST /session` (app token only) → `{"token":str,"idle_timeout_s":int,"expires_at":iso}`.
  Session tokens are in memory only (backend and UI; never localStorage). Sliding idle timeout =
  config `lock_idle_minutes` (int 1–240, default 15); absolute cap 12 h. `DELETE /session` (app token)
  revokes all sessions; `DELETE /session/current` (session token) locks this session.
  Session scope = every endpoint except `POST /session` / `DELETE /session` / `POST /audit/event`.
- Unauthenticated: `GET /health` only (no user content in it). Missing/invalid/expired token → 401
  `{"detail":"locked"}`. Tokens compared in constant time. Requests whose `Host` isn't
  `127.0.0.1:<port>` / `localhost:<port>` → 403 (DNS rebinding). CORS list unchanged.
- Rust (Tauri commands): `unlock() -> {token, idle_timeout_s}` = LocalAuthentication
  `deviceOwnerAuthentication` (Touch ID or Mac password), then `POST /session`; `lock()`;
  `get_lock_state() -> {locked: bool}`. Rust locks (DELETE /session + emits event `locked`) on screen
  lock (`com.apple.screenIsLocked`), system sleep, and tray "Lock Polytone". Hotkey and typing capture
  keep working while locked (they use the app token).
- UI: any 401 → lock screen ("Unlock with Touch ID"); idle timer on its side too, for an immediate lock.

### Audit log (`backend/security/audit.py`, `data/audit.sqlite3`)
- Table `audit_log(seq INTEGER PRIMARY KEY, ts TEXT, event TEXT, actor TEXT, detail TEXT /*JSON*/, prev TEXT, mac TEXT)`.
  `mac` = HMAC-SHA256(subkey("audit"), prev || canonical-JSON(seq, ts, event, actor, detail)); genesis prev = 64×"0".
  SQLite triggers abort UPDATE and DELETE. Head (`seq`, `mac`) also kept in `data/.audit_head`
  (0600) so truncation is detected. Never contains user text: lengths, counts, ids, app bundle ids only.
- Events (at least): `session.unlock`, `session.unlock_failed`, `session.lock`, `session.expired`,
  `auth.denied`, `generate` (persona, chars in/out, model, latency), `ingest` (source, accepted/rejected),
  `config.change` (keys changed, not values of secrets), `capture.start`/`capture.stop`,
  `data.wipe`, `data.retention_purge`, `key.created`, `migration.encrypted`.
- `GET /audit?limit=&before_seq=` → `{"entries":[...], "head_seq":int}`; `GET /audit/verify` →
  `{"ok":bool,"count":int,"first_bad_seq":int|null,"reason":str|null}`; `POST /audit/event`
  (app token only) `{event, detail}` for Rust-side events (actor `app`).

### Network
- Outbound HTTPS (Anthropic, Google APIs): TLS 1.3 minimum, certificate verification on (default CA store).
- Loopback (UI/Rust ↔ backend, backend ↔ Neo4j on 127.0.0.1): no TLS; protected by tokens / password.
  Neo4j ports stay bound to 127.0.0.1.
- Neo4j password: `scripts/rotate-neo4j-password.sh` generates a random one, applies it in the running
  container, writes it to `.env` (0600). `dev.sh` runs it when `.env` still has the default.

### Retention and files
- Config `retention_days` (0 = keep forever, default 0): daily purge of `samples` from `typed`/`wispr`,
  `draft_edits` and `drafts` older than N days (style graph aggregates stay). `POST /data/wipe`
  `{"confirm":"delete everything"}` deletes all samples, drafts, draft_edits and the style graph.
- `data/` is 0700, files in it 0600 (including `backend.log`, `gmail_token.json`, `.env`).
  Logs never contain user text.
- `ANTHROPIC_API_KEY` may live in the Keychain (service `com.polytone.app`, account `anthropic-api-key`)
  instead of `.env`; env wins when both exist.
