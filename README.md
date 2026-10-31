# Polytone

Polytone is a macOS menu-bar app that rewrites rough intent into **your own** writing style, in any app.

Type something rough (`- tell dana launch on track, deck thurs`) in Slack, Mail, Notes or anywhere else,
press **⌘⇧P**, and Polytone replaces it with a draft that sounds like you wrote it: your sentence
length, your punctuation habits, your openers, your casing. It learns that style only from text you
actually wrote yourself: text you paste in, your sent Gmail, raw Wispr Flow dictation, and files you drop
into a folder. Every draft gets a 0–100 style score showing how close it sits to your real writing.

Personas (Boss, Friend, Investor, Default, plus any tag you ingest samples under) keep separate
registers. Writing to your manager and texting a friend shouldn't sound the same.

## Architecture

```
 any app (Slack, Mail, …)                         Polytone.app  (Tauri v2, src-tauri/, Rust)
 ┌──────────────────────┐   ⌘⇧P   ┌──────────────────────────────────────────────────────────┐
 │ rough intent ▌       │ ──────▶ │ global hotkey → capture (⌘C / ⌘A⌘C, clipboard snapshot)   │
 │                      │ ◀────── │ inject (⌘V or typed) → restore clipboard                  │
 └──────────────────────┘  draft  │ menu-bar tray: persona · pause · open                     │
                                  │ React UI (src/): Feed · Scoreboard · Graph · Settings     │
                                  └───────────────┬──────────────────────────────────────────┘
                                                  │ HTTP 127.0.0.1:8765  (autostarted: uv run polytone-api)
                                  ┌───────────────▼──────────────────────────────────────────┐
                                  │ Python backend (backend/, FastAPI)                        │
                                  │  generate graph (LangGraph):                              │
                                  │    capture → constraints → prompt → LLM → post → record → score
                                  │  ingest graph:  validate → FIREWALL → extract → write_graph │
                                  │  sources: manual · gmail (sent only) · wispr (raw) · watcher│
                                  │  scorer: stylometric distance + optional Qwen3 embedding   │
                                  └──────┬───────────────────────────────┬───────────────────┘
                                         │ bolt :7687                    │
                              ┌──────────▼─────────┐          ┌──────────▼──────────────┐
                              │ Neo4j (docker)     │          │ SQLite data/            │
                              │ style graph: Word, │          │ samples, drafts+scores, │
                              │ SentenceStructure, │          │ config, ingest runs     │
                              │ Punctuation,Intent │          └─────────────────────────┘
                              └────────────────────┘
```

`CONTRACTS.md` is the binding spec for every boundary: the HTTP API, the hotkey loop, the graph schema
and the module layout.

## Quick start

Requirements: macOS, Docker, [uv](https://docs.astral.sh/uv/), Node 20+ with pnpm, and Rust (stable).

```bash
cp .env.example .env                              # then set ANTHROPIC_API_KEY=sk-ant-…
npm run tauri:dev                                 # or: pnpm start
```

`npm run tauri:dev` runs `scripts/dev.sh`, which installs missing JS/Python deps, starts Docker and Neo4j,
stops leftovers from an earlier run (a stale backend or a vite still holding port 1420), then runs
`pnpm tauri dev`. The app starts the backend itself (output in the terminal, or `data/backend.log` when
launched from Finder) and the backend exits when the app does, even if the app is force-quit.

Launching opens the Polytone window; it also lives in the menu bar. Closing the window keeps it running.
Opening the app again while it runs brings the window back instead of starting a second copy. Without
Accessibility permission it opens on the Settings tab with a three-step permissions guide.

- With no `ANTHROPIC_API_KEY` (or with `POLYTONE_LLM=mock`), a deterministic **mock LLM** produces drafts.
  This is useful for development. If you set `POLYTONE_LLM=anthropic` without a key, `/generate` answers
  `502` and the header shows "no API key". Polytone never quietly falls back to the mock.
- To run the backend yourself: `uv run polytone-api`. The app detects a running backend and
  uses it. A backend the app spawned is stopped when the app quits.
- Release build: `pnpm tauri build --bundles app` produces `src-tauri/target/release/bundle/macos/Polytone.app`.
  The DMG target scripts Finder through AppleEvents, so it needs an interactive session. When the app
  runs outside the repo, set `POLYTONE_REPO=/path/to/polytone` so it can find the backend.

### Granting macOS permissions

Polytone has to send ⌘C/⌘A/⌘V to other apps, so macOS has to trust it:

1. **System Settings → Privacy & Security → Accessibility**: enable **Polytone**. Under
   `pnpm tauri dev`, enable the app that launches it (your terminal, or the `polytone` dev binary).
   Settings → *Open System Settings* takes you straight to the pane.
2. **Privacy & Security → Input Monitoring**: enable it too if the hotkey fires but nothing is copied or pasted.
3. Return to Polytone. The card re-checks when the window regains focus. Some macOS versions only
   apply the change after you relaunch the app.

Rebuilt dev binaries change their code signature, so macOS may ask you to grant the permission again.
To reset it, toggle the entry off and on, or remove it with `−` and add it back.

## Using it

1. Type or select rough intent in any text field.
2. Press **⌘⇧P**. You can change the hotkey in Settings or pause it from the tray or Settings.
3. Polytone copies the selection. If nothing is selected it copies the whole field, and if the field is
   empty it uses what was already on the clipboard. It then generates a draft, pastes it over the text,
   and puts your clipboard back.

The whole loop should finish within 5 s. If generation fails or times out, you get a notification and
your text is left untouched. Inject mode **Type** types the characters directly, for apps that block paste.

**Clipboard restore.** On macOS, Polytone snapshots every pasteboard item in every representation
(plain and rich text, images, file references, app-private types) and writes it all back afterwards.
Two limitations: lazily promised data (file promises) is kept only if the source app hands it over when
asked, and snapshots over 64 MB fall back to text, then image. If the clipboard can't be read at all,
Polytone leaves the draft on it instead of wiping it.

## Ingestion sources

Everything goes through the same pipeline, `validate → firewall → extract → write_graph → record`.
Quoted reply chains and signatures are stripped first, and only the cleaned text is learned and stored.

| Source | How | Notes |
|---|---|---|
| **Manual** | Feed tab: paste text, tag a persona, ⌘↵ | `POST /ingest/manual` |
| **Gmail** | Put a Google OAuth *Desktop app* client JSON at `./credentials.json` (`GMAIL_CREDENTIALS_PATH`), then run `uv run python -m backend.mcp_servers.gmail_outbound auth` once | Searches `in:sent from:me` only. Each message must carry the `SENT` label and come from your address. Incremental. Press *Run* in Feed, or `… gmail_outbound sync`. |
| **Wispr Flow** | Automatic and live if Wispr Flow is installed: the backend watches its local history DB (`~/Library/Application Support/Wispr Flow/flow.sqlite`, read from a snapshot copy) and learns each dictation ~1.5 s after it lands | **Raw** ASR transcripts only, never the formatted or AI-polished text, under the `Default` persona tagged `kind: "spoken"`. Archived dictations count. `POLYTONE_WISPR_LIVE=0` stops the live watch (*Run* still works); `POLYTONE_WISPR_LOCAL=0` turns local history off. Optional WebSocket: `WISPR_WS_URL` + `WISPR_API_KEY`. |
| **Watch folder** | Drop files into `data/inbox` (`POLYTONE_WATCH_DIR`). The backend watches it continuously (`POLYTONE_WATCHER=0` turns that off) | `.txt`/`.md` (paragraphs), `.mbox` (only messages **From:** one of `POLYTONE_USER_EMAILS`, comma-separated, or your Gmail address), iMessage `chat.db` and similar SQLite dumps (`is_from_me = 1` rows only). Files are tracked by sha256, so rescans are idempotent. |

Each source module is also a stdio MCP server. See `backend/mcp_servers/README.md`.

### The firewall guarantee

**Generated text never trains the model of your style.** Drafts are written to SQLite `drafts` only,
never to the graph. Every incoming sample has to pass `ingestion/firewall.py`, which rejects, and logs a
reason for:

- anything flagged `generated` or with `origin="polytone"`
- inbound mail, and any per-source provenance failure (Gmail not sent by you, a Wispr transcript that
  isn't raw, an `.mbox` message not from you)
- text that matches a stored draft: same sha256, a draft contained in the text or the text contained in a
  draft, shingle Jaccard ≥ 0.8, or ≥ 80% of a draft's shingles present
- duplicates and fragments (< 3 words or < 10 characters)

If you paste one of Polytone's own drafts into Feed, you get `rejected: matches a Polytone-generated draft`.

Wispr Flow's output is kept out the same way: when you dictate into Slack and press Return, typing
capture sees Wispr's AI-formatted text in the message. Polytone removes every span Wispr inserted in
that app in the last 30 minutes (verbatim, or ≥ 80% of its shingles) and learns only what you typed
around it. If nothing learnable is left, the message is rejected as `wispr_inserted`. Your dictation is
still learned, from its raw transcript.

## Style score

`evaluation/style_distance.py` compares a draft with the persona's human samples on sentence length,
punctuation, lexical choice, function words and (with `POLYTONE_EMBEDDINGS=qwen`) a Qwen3-Embedding style
vector. Each component is a 0–1 distance. The overall score is `100 × (1 − distance)`, plus a
`confidence` (low/medium/high) that reflects how many samples back it and how long the draft is.

Scoring runs in a background thread, so it never delays a draft. The embedding model is warmed in the
background when the backend starts (from the local Hugging Face cache, with no network round trips once it
has been downloaded). Run `uv run python -m backend.evaluation.calibrate` for sanity numbers.

## Configuration

`.env` (see `.env.example`): `ANTHROPIC_API_KEY`, `POLYTONE_MODEL` (default `claude-sonnet-5-5`),
`POLYTONE_LLM` (`anthropic`/`mock`), `POLYTONE_EFFORT` (`low` default; higher = more thinking, slower drafts), `NEO4J_*`, `POLYTONE_API_PORT` (8765), `POLYTONE_DATA_DIR`,
`POLYTONE_WATCH_DIR`, `POLYTONE_USER_EMAILS`, `POLYTONE_WISPR_LOCAL`, `POLYTONE_WISPR_LIVE`, `POLYTONE_WATCHER`,
`POLYTONE_EMBEDDINGS` (`qwen`/`none`), `GMAIL_*`, `WISPR_*`. App-side variables: `POLYTONE_REPO` (backend
location for a bundled app) and `POLYTONE_START_HIDDEN=1` (start in the menu bar only; the window still opens when permissions are missing).

## Running tests

```bash
uv run pytest -q                 # backend: offline (mock LLM, no embeddings); Neo4j tests skip if it's down
uv run ruff check backend
pnpm build                       # tsc + vite
cd src-tauri && cargo test && cargo clippy --all-targets
cargo test -- --ignored real_pasteboard   # optional: round-trips an image through the real macOS clipboard
```

The Neo4j integration tests never touch your real graph: they skip unless you point them at a scratch container:
`docker run -d --rm --name polytone-test -p 7688:7687 -e NEO4J_AUTH=neo4j/test-pass neo4j:5-community`, then
`POLYTONE_TEST_NEO4J_URI=bolt://localhost:7688 uv run pytest -q`.

Benchmarks (mock LLM, scratch SQLite, never touch `data/`):

```bash
uv run python scripts/bench_generate.py --http                 # per-node p50/p95 of POST /generate
uv run python scripts/bench_generate.py --neo4j bolt://localhost:7688 --ingest-every 5   # scratch Neo4j (wiped!)
uv run python scripts/bench_startup.py --embeddings qwen       # launch → /health, → embeddings warm (port 8775)
```

## Troubleshooting

- **Header says "offline"**: the backend isn't running. Start it with `uv run polytone-api` and read its
  output. The app autostart looks for `uv` on PATH, in `~/.local/bin`, `~/.cargo/bin` and `/opt/homebrew/bin`.
- **"no graph" / "memory graph"**: Neo4j is unreachable (`docker compose up -d`, then `docker ps`). The
  backend falls back to an in-memory graph that is **not persisted**.
- **"no API key" / hotkey reports "The model failed"**: add `ANTHROPIC_API_KEY` to `.env` and restart the
  backend, or set `POLYTONE_LLM=mock`.
- **Hotkey does nothing**: check Accessibility and Input Monitoring (above), and make sure the hotkey
  isn't paused (tray → *Pause hotkey*, or Settings → Hotkey). Another app may already own the shortcut;
  pick a different one in Settings.
- **Draft pasted, but my clipboard changed**: see *Clipboard restore* above. Only content that
  couldn't be read is left replaced.
- **Gmail shows "needs credentials"**: follow the exact command shown in the Feed row.
- **First draft's score is missing**: scores arrive a moment after the draft. The Scoreboard refreshes
  every 15 s, or after each hotkey run.
- **Embeddings unavailable**: install with `uv sync --extra embeddings`. The model downloads once from
  Hugging Face. Set `POLYTONE_EMBEDDINGS=none` for stylometric-only scoring.
