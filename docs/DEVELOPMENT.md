# Developing Polytone

For what Polytone is and how to install it, see the [README](../README.md). For how it works, see
[ARCHITECTURE.md](ARCHITECTURE.md).

## Repository layout

| Path | What |
|---|---|
| `src-tauri/` | Rust shell (Tauri v2): hotkey, capture and inject, clipboard, tray, typing capture, backend process |
| `src/` | React and TypeScript UI |
| `backend/` | Python backend (FastAPI, LangGraph), with tests in `backend/tests/` |
| `scripts/` | `dev.sh` (one-command launch), password rotation, benchmarks |
| `CONTRACTS.md` | The binding spec for every boundary. Update it with any interface change. |
| `orchestrator.py`, `validate.py` | The unattended agent loop and its checks (see below) |

## Running in development

```bash
pnpm start            # = npm run tauri:dev = bash scripts/dev.sh
```

`scripts/dev.sh` does the following, then runs `pnpm tauri dev`:
- installs missing JS and Python dependencies (`uv sync --extra embeddings --extra gmail`)
- creates `.env` if needed
- starts Docker and Neo4j, and rotates the default Neo4j password
- stops leftovers from an earlier run: a stale backend, a vite still holding port 1420, or a running Polytone

The app starts the backend itself. Its output goes to the terminal, or to `data/backend.log` when
launched from Finder.

Other ways to run the parts:

```bash
uv run polytone-api          # backend alone; a running app detects and uses it
docker compose up -d         # Neo4j alone
```

## Tests

```bash
uv run pytest -q                 # backend: offline (mock LLM, no embeddings); Neo4j tests skip if it's down
uv run ruff check backend
pnpm build                       # tsc + vite
cd src-tauri && cargo test && cargo clippy --all-targets
cargo test -- --ignored real_pasteboard   # optional: round-trips an image through the real macOS clipboard
```

Or run everything CI runs: `python3 validate.py`.

**Neo4j integration tests never touch your real graph.** They skip unless you point them at a scratch container:

```bash
docker run -d --rm --name polytone-test -p 7688:7687 -e NEO4J_AUTH=neo4j/test-pass neo4j:5-community
POLYTONE_TEST_NEO4J_URI=bolt://localhost:7688 uv run pytest -q
```

Tests and benchmarks never touch the real Keychain. They use a fixed or random `POLYTONE_DATA_KEY`.

## Benchmarks

These use the mock LLM and scratch SQLite, and never touch `data/`.

```bash
uv run python scripts/bench_generate.py --http                 # per-node p50/p95 of POST /generate
uv run python scripts/bench_generate.py --neo4j bolt://localhost:7688 --ingest-every 5   # scratch Neo4j (wiped!)
uv run python scripts/bench_startup.py --embeddings qwen       # launch → /health, → embeddings warm (port 8775)
uv run python -m backend.evaluation.calibrate                  # style-score sanity numbers
```

## Release build

```bash
pnpm tauri build --bundles app
# → src-tauri/target/release/bundle/macos/Polytone.app
```

The DMG target scripts Finder through AppleEvents, so it needs an interactive session. When the
app runs outside the repo, set `POLYTONE_REPO=/path/to/polytone` so it can find the backend.

Rebuilt dev binaries change their code signature, so macOS may ask for Accessibility permission
again. Toggle the entry off and on, or remove it with `−` and add it back.

## Contributing workflow

`main` is never committed to directly. For each change:

1. `git switch -c <type>/<short-name>` from an up-to-date `main`.
2. Make the change, update `CONTRACTS.md` if an interface changed, and run the tests.
3. `git push -u origin HEAD`, then `gh pr create --fill`.
4. Wait for CI (`gh pr checks --watch`). It runs backend pytest and ruff, the frontend build, and
   macOS `cargo test` and `clippy` when `src-tauri/` changed. Never merge a red PR.
5. `gh pr merge --squash --delete-branch`.

Never commit `.env`, `credentials.json`, `token.json` or anything under `data/`.

## Unattended agent loop

`orchestrator.py` runs headless Claude Code on tasks from `progress.json` (git-ignored) as an
implementor/evaluator pair:

1. The implementor makes the change.
2. `validate.py`, the same checks as CI, must pass.
3. A read-only evaluator agent reviews the diff against the task.

Failures and rejections are fed back until the task passes or hits `max_iterations`.

```bash
git worktree add ../polytone-agent main     # run it in its own worktree, inside tmux
cd ../polytone-agent
python3 orchestrator.py new "Task title" --details "Acceptance criteria: …"
python3 orchestrator.py run --watch --pr --routine
python3 orchestrator.py status
```

With `--routine`, it also queues routine work when idle (a validation sweep, then tests, then a bug
hunt), every 6 hours by default. It queues none while 2 routine PRs are open. Agents commit only on
`agent/<task>`, and they never edit `validate.py`, `orchestrator.py`, `progress.json` or `.github/`.
See [`CLAUDE.md`](../CLAUDE.md).
