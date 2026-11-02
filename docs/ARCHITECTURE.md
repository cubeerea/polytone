# Polytone architecture

How Polytone works under the hood. For setup and everyday use, see the [README](../README.md).
[`CONTRACTS.md`](../CONTRACTS.md) is the binding spec for every boundary: the HTTP API, the hotkey
loop, the graph schema and the module layout. This doc gives the overview.

- [Components](#components)
- [The hotkey loop](#the-hotkey-loop)
- [Ingestion](#ingestion)
- [The firewall](#the-firewall)
- [The style graph](#the-style-graph)
- [Generation](#generation)
- [Continual learning](#continual-learning)
- [Style score](#style-score)
- [Security and privacy](#security-and-privacy)

## Components

```
 any app (Slack, Mail, …)                         Polytone.app  (Tauri v2, src-tauri/, Rust)
 ┌──────────────────────┐   ⌘⇧P   ┌──────────────────────────────────────────────────────────┐
 │ rough intent ▌       │ ──────▶ │ global hotkey → capture (⌘C / ⌘A⌘C, clipboard snapshot)   │
 │                      │ ◀────── │ inject (⌘V or typed) → restore clipboard                  │
 └──────────────────────┘  draft  │ menu-bar tray: persona · pause · open                     │
                                  │ typing capture (opt-in, listen-only event tap)            │
                                  │ React UI (src/): Drafts · Style · Settings                │
                                  └───────────────┬──────────────────────────────────────────┘
                                                  │ HTTP 127.0.0.1:8765, bearer token
                                                  │ (autostarted: uv run polytone-api)
                                  ┌───────────────▼──────────────────────────────────────────┐
                                  │ Python backend (backend/, FastAPI)                        │
                                  │  generate graph (LangGraph):                              │
                                  │    capture → constraints → prompt → LLM → post → record → score
                                  │  ingest graph:  validate → FIREWALL → extract → write_graph │
                                  │  sources: manual · gmail (sent only) · wispr (raw) ·      │
                                  │           watch folder · typed                            │
                                  │  learning: edit corrections · decay · weekly evaluation   │
                                  │  scorer: stylometric distance + optional Qwen3 embedding  │
                                  └──────┬───────────────────────────────┬───────────────────┘
                                         │ bolt :7687                    │
                              ┌──────────▼─────────┐          ┌──────────▼──────────────┐
                              │ Neo4j (docker)     │          │ SQLite data/            │
                              │ style graph: Word, │          │ samples, drafts+scores, │
                              │ SentenceStructure, │          │ corrections, snapshots, │
                              │ Punctuation,Intent │          │ config, ingest runs     │
                              └────────────────────┘          └─────────────────────────┘
```

| Layer | Where | Role |
|---|---|---|
| Shell | `src-tauri/` (Rust, Tauri v2) | Global hotkey, capture and inject, clipboard snapshot and restore, tray, typing capture, starting and stopping the backend, Keychain, unlocking with Touch ID |
| UI | `src/` (React, TypeScript, Tailwind, d3) | Drafts, Style (graph view and sources), Settings |
| Backend | `backend/` (Python 3.11–3.12, FastAPI, LangGraph) | Ingestion, firewall, style graph, generation, scoring, learning, security |
| Style graph | Neo4j 5 community (`docker-compose.yml`) | Aggregated style statistics. Falls back to an in-memory store, which is not persisted, when Neo4j is unreachable |
| Records | SQLite under `data/` | Samples, drafts, scores, edits, corrections, snapshots, config, audit log |

The app starts the backend itself and the backend exits when the app does, even after a force-quit.
If a backend is already running (for example `uv run polytone-api`), the app uses it instead.

## The hotkey loop

1. **Capture.** The shell snapshots the clipboard, then sends ⌘C. If nothing was selected it sends
   ⌘A ⌘C, and if the field is empty it uses what was already on the clipboard.
2. **Generate.** The shell calls `POST /generate` with the text and the active persona.
3. **Inject.** The shell pastes the draft with ⌘V, or types it key by key in *Type* mode, then
   restores the clipboard.

The loop targets under 5 s. On failure or timeout it shows a notification and leaves the user's text untouched.

**Clipboard restore.** Polytone snapshots every pasteboard item in every representation: plain and
rich text, images, file references and app-private types. It writes them all back afterwards, with
two limits:
- Lazily promised data (file promises) is kept only if the source app hands it over when asked.
- Snapshots over 64 MB fall back to text, then image.

If the clipboard can't be read at all, Polytone leaves the draft on it rather than wiping it.

## Ingestion

Every source runs the same pipeline: `validate → firewall → extract → write_graph → record`.
Quoted reply chains and signatures are stripped first, and only the cleaned text is learned and stored.

| Source | Mechanism | Provenance rule |
|---|---|---|
| **Manual** | `POST /ingest/manual` from the Style tab | Anything you paste, minus Polytone drafts |
| **Gmail** | `backend/mcp_servers/gmail_outbound`, searching `in:sent from:me`, incremental | Each message must carry the `SENT` label and come from your address |
| **Wispr Flow** | Watches the local history DB (`~/Library/Application Support/Wispr Flow/flow.sqlite`, read from a snapshot copy) and learns each dictation about 1.5 s after it lands. Optional WebSocket via `WISPR_WS_URL` and `WISPR_API_KEY` | Raw ASR transcripts only, never formatted or AI-polished text. Stored under `Default`, tagged `kind: "spoken"` |
| **Watch folder** | `data/inbox` (`POLYTONE_WATCH_DIR`), watched continuously. Files are tracked by sha256, so rescans are idempotent | `.txt`/`.md` split by paragraph. `.mbox` keeps only messages **From:** one of `POLYTONE_USER_EMAILS` or your Gmail address. iMessage `chat.db` keeps only `is_from_me = 1` rows |
| **Typed** | Opt-in. A listen-only `CGEventTap` in the shell reads the focused text field of allowlisted apps and posts the text to `POST /ingest/typed` when a send is confirmed | Never secure or search fields, never while secure input is on, drops fewer than 3 words and slash commands |

Each source module is also a stdio MCP server. See [`backend/mcp_servers/README.md`](../backend/mcp_servers/README.md).
The typing capture rules (send detection, browser and Electron handling, edit tracking after an
inject) are specified in `CONTRACTS.md` → *Typing capture*.

## The firewall

**Generated text never trains the style model.** Drafts are written only to the SQLite `drafts`
table, never to the graph. Every incoming sample has to pass `backend/ingestion/firewall.py`, which
rejects the following and logs a reason:

- anything flagged `generated` or with `origin="polytone"`
- inbound mail, and any per-source provenance failure (Gmail not sent by you, a Wispr transcript
  that isn't raw, an `.mbox` message not from you)
- text that matches a stored draft: the same sha256, a draft contained in the text or the text
  contained in a draft, shingle Jaccard ≥ 0.8, or ≥ 80% of a draft's shingles present
- duplicates and fragments (fewer than 3 words or 10 characters)

Wispr Flow's output is kept out the same way. When you dictate into Slack and press Return, typing
capture sees Wispr's AI-formatted text in the message. Polytone removes every span Wispr inserted in
that app in the last 30 minutes, whether verbatim or with ≥ 80% of its shingles present, and learns
only what you typed around it. If nothing learnable is left, the message is rejected as
`wispr_inserted`. The dictation itself is still learned, from its raw transcript.

## The style graph

`backend/ingestion/text.py` segments each sample. The extractor records words, word bigrams
(`FOLLOWED_BY`), sentence structures and openers, punctuation marks and per-persona context
(`IntentContext`). The schema is in `CONTRACTS.md` → *Neo4j schema*.

**Bounded writes.** `backend/graph/trim.py` caps what one sample can write, so very large or
repetitive input can't overload the graph:

| Cap | Limit per sample |
|---|---|
| Words | 3000 |
| Bigrams | 6000 |
| Word–structure links | 2000 |
| Structures | 500 |
| Keyed items | 200 |
| Fan-out | 64 |

Across the whole graph, high-frequency words such as "the" and "and" keep at most 256 followers
and predecessors. Writes go to Neo4j in chunks of 1000.

**Constraints.** `backend/graph/constraints.py` turns the graph into concrete rules: sentence
length, punctuation rates, contractions, casing, preferred openers and signature words. Persona
rules are blended with global rules by `alpha = n / (n + 3)`, where `n` is the persona's sample
count, so a new persona leans on your overall style until it has its own data.

## Generation

`backend/langgraph/generate_graph.py` runs `capture → constraints → prompt → LLM → post → record → score`.

- **Prompt** (`backend/langgraph/prompts.py`):
  - The persona tone, the constraints as **HARD STYLE RULES**, and words to avoid go in the stable,
    cached part of the system prompt.
  - Two or three few-shot excerpts are picked from the persona's 200 most recent samples
    (`backend/langgraph/examples.py`). Excerpts shaped like drafts you accepted are preferred.
- **Model:**
  - **Best** uses `POLYTONE_MODEL`, `claude-sonnet-5-5` by default.
  - **Fast** uses `claude-haiku-5-5`.
  - With no API key, or with `POLYTONE_LLM=mock`, a deterministic mock model writes the drafts.
  - With `POLYTONE_LLM=anthropic` and no key, `/generate` returns `502`. Polytone never quietly
    falls back to the mock.
- **Post-processing** enforces the hard rules that can be checked mechanically.
- **Record** stores the draft encrypted in SQLite.
- **Score** runs in a background thread, so it never delays a draft.

## Continual learning

Polytone never fine-tunes a model. It learns by updating the *memory* the prompt is built from: graph
statistics, the few-shot pool and a per-persona corrections layer. All three can be inspected and reversed.

### Learning from edits

When you edit an injected draft before sending it, typing capture reports what you actually sent.
`backend/learning/edits.py` matches it to the draft it came from. A match needs shingle containment
≥ 0.3 within 600 s. Polytone then learns only the *differences*:

- sentences you shortened or lengthened
- punctuation you removed or added
- contractions and casing
- words you keep deleting

These are smoothed per persona, with weight `max(1/n, 0.2)`, so recent edits count most, and stored
encrypted in the `corrections` table. After 3 edits they adjust the style rules:

- Length and rates move by at most 30%.
- A punctuation mark you keep deleting becomes "never use".
- Words you delete from at least 60% of the drafts that contain them go on a "do not use" list,
  up to 12 words.

The sent text itself is never learned, because it is partly Polytone's, so the graph and samples
are untouched. `GET /learning` shows what is currently being corrected.

### Recency and forgetting

Every increment to the graph is scaled by a "growing unit", `s(t) = 2^((t − t0) / H)`. The half-life
H is `POLYTONE_DECAY_HALF_LIFE_DAYS`, 90 days by default. Reads divide by `s(now)`, so a habit from
three months ago counts half as much as one from today, and one from six months ago a quarter.

- **No rewriting.** Nothing is re-learned or rewritten on each read.
- **Pauses don't erode your style.** If you stop writing for a while, your style stays as it was,
  because only the *relative* age of samples matters.
- **Counts stay raw.** `sample_count` stays a plain count for the persona blend. A separate decayed
  `sample_weight` scales thresholds.
- **Rebase.** When the scale passes 1e6, a batched, crash-resumable job divides every weight down
  and resets `t0`.
- **Prune.** Once a day and at start-up, `decay_maintenance()` deletes edges whose effective weight
  has fallen below 0.05.
- **Settings.** `0` turns decay off. Changing the half-life is safe, because the graph is converted
  at the next start.

The Neo4j rebase and prune use `CALL { … } IN TRANSACTIONS`, which needs Neo4j 5.23 or later.

### Evaluation, snapshots and rollback

`backend/learning/evaluate.py` runs every `learning_eval_days` days (default 7, 0 means manual
only). A persona is checked when it has at least 5 samples *and* something new since the last check.

1. **Write test drafts.** Polytone drafts six fixed, neutral test intents three ways:
   - with the current rules (graph plus corrections)
   - with the graph alone
   - with the last accepted snapshot
2. **Score them.** Each set is scored against the persona's 20 newest samples, which act as the holdout.
3. **Decide.** The current rules are kept unless another set wins by at least 2 points, with
   hysteresis so small differences never flip the decision.
   - If the old snapshot wins, drafts roll back to it until the next check.
   - If the graph alone wins, corrections are paused.

Snapshots (`constraint_snapshots`) and the current decision (`learning_state`) are stored encrypted.
`DELETE /learning/state` undoes a rollback or pause. With a real model, a check costs up to 18 short
calls per persona.

**Draft outcomes.** Each draft records what happened to it: `accepted`, `regenerated`, `edited` or
`discarded`. Accepted drafts boost the few-shot weight of samples with similar structure, by up to 0.25.

**Known limitations.**
- Nothing in the UI marks a draft "discarded" yet.
- Rolled-back rules stay frozen until the next evaluation.
- The holdout samples are also in the graph.
- Evaluation scores a draft against samples it was conditioned on, so it measures consistency, not
  independent ground truth.

## Style score

`backend/evaluation/style_distance.py` compares a draft with the persona's human samples on five
components. Each is a 0–1 distance:

| Component | Weight |
|---|---|
| Function words | 0.25 |
| Embedding (Qwen3-Embedding-0.6B, with `POLYTONE_EMBEDDINGS=qwen`) | 0.25 |
| Punctuation | 0.20 |
| Lexical choice | 0.15 |
| Sentence length | 0.15 |

The score is `100 × (1 − distance)`. A `confidence` of low, medium or high reflects how many samples
back it and how long the draft is. The embedding model is warmed in the background at start-up from
the local Hugging Face cache. Evaluation runs score without embeddings. For sanity numbers, run
`uv run python -m backend.evaluation.calibrate`.

## Security and privacy

The full spec is in `CONTRACTS.md` → *Security*.

- **Encryption at rest.**
  - Everything you wrote is stored in SQLite as AES-256-GCM ciphertext (`enc1:…`): samples, drafts
    and their intents, post-inject edits, corrections, snapshots and provenance.
  - The Neo4j graph holds only blind indexes (`idx1:…`, keyed hashes used for lookups) plus ciphertext.
  - The 256-bit key lives in the macOS Keychain (`com.polytone.app` / `data-key-v1`) and is created
    on first start.
  - Plaintext data from older versions is encrypted in place on first start and recorded as
    `migration.encrypted` in the audit log.
  - Neo4j doesn't compact its store files, so old plaintext pages may remain until reused. For a
    clean store, wipe and re-ingest, or recreate the `polytone_neo4j_data` volume. SQLite runs with
    `secure_delete=ON`.
- **Locked API.**
  - Every endpoint except `GET /health` needs a bearer token. The Rust shell holds the app token
    (`POLYTONE_API_TOKEN`, also written to `data/.api_token` with mode 0600).
  - The UI gets a short-lived session after Touch ID or your Mac password. It locks after
    `lock_idle_minutes` (default 15), on screen lock and on sleep.
  - Requests with a non-loopback `Host` are refused.
  - To call the API by hand: `curl -H "Authorization: Bearer $(cat data/.api_token)" http://127.0.0.1:8765/stats`.
- **Audit log.**
  - `data/audit.sqlite3` is an append-only, HMAC-chained log of security events: unlock and lock,
    denied requests, generations, ingests, config changes, wipes and purges.
  - It records counts and ids only, never text.
  - `GET /audit/verify` re-checks the chain and reports the first altered or missing row.
- **Retention.**
  - `retention_days` (0 keeps data forever) purges typed and Wispr samples, drafts and edits older
    than N days, at start-up and daily.
  - `POST /data/wipe {"confirm":"delete everything"}` deletes all samples, drafts, edits,
    corrections, snapshots and the style graph.
- **Network.** Calls to Anthropic and Google require TLS 1.3. `ANTHROPIC_API_KEY` may live in the
  Keychain (`-s com.polytone.app -a anthropic-api-key`). If both are set, `.env` wins.
- **Files.** `data/` is mode 0700 and its files are 0600, and `.env` is 0600. Logs never contain your text.
- **Neo4j password.**
  - On the first run, `scripts/dev.sh` replaces the shipped `polytone-local` password with a random
    one, using `scripts/rotate-neo4j-password.sh`.
  - The script changes the password in the running container first, and only then rewrites `.env`.
  - Run it any time to rotate the password again. Neo4j stays bound to 127.0.0.1.
