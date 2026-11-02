# Configuration reference

Polytone reads environment variables from `.env` in the repo folder (copy `.env.example` to start).
Restart the app after changing them. Settings you change in the app are stored in SQLite and
listed [at the end](#in-app-settings).

## Model

| Variable | Default | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | (empty) | Your Anthropic API key. It can live in the Keychain instead (`security add-generic-password -s com.polytone.app -a anthropic-api-key -w sk-ant-…`). If both are set, `.env` wins. |
| `POLYTONE_MODEL` | `claude-sonnet-5-5` | Model for **Best** drafts. **Fast** drafts always use `claude-haiku-5-5`. |
| `POLYTONE_LLM` | `anthropic` when a key is present, otherwise `mock` | `mock` gives offline, deterministic drafts for development. `anthropic` without a key fails loudly instead of falling back. |
| `POLYTONE_EFFORT` | `low` | Thinking effort: `low`, `medium`, `high`, `xhigh` or `max`. Higher means slower drafts. |

## Learning

| Variable | Default | Meaning |
|---|---|---|
| `POLYTONE_DECAY_HALF_LIFE_DAYS` | `90` | Writing this many days old counts half as much as writing from today. `0` gives every sample equal weight forever. |
| `POLYTONE_EMBEDDINGS` | `qwen` | `qwen` adds a Qwen3-Embedding style vector to the score (`uv sync --extra embeddings`). `none` uses stylometric features only. |

## Sources

| Variable | Default | Meaning |
|---|---|---|
| `GMAIL_CREDENTIALS_PATH` | `./credentials.json` | Google OAuth *Desktop app* client JSON |
| `GMAIL_TOKEN_PATH` | `./data/gmail_token.json` | Where the Gmail token is stored after `auth` |
| `POLYTONE_WATCH_DIR` | `./data/inbox` | Folder the watcher learns from |
| `POLYTONE_WATCHER` | `1` | Watch that folder continuously. `0` scans only when you press **Run**. |
| `POLYTONE_USER_EMAILS` | (empty) | Your addresses, comma-separated. `.mbox` files keep only messages from these addresses (or your Gmail address). |
| `POLYTONE_WISPR_LOCAL` | `1` | Read Wispr Flow's local history. `0` turns it off. |
| `POLYTONE_WISPR_LIVE` | `1` | Watch Wispr Flow's history live. `0` reads it only when you press **Run**. |
| `WISPR_TYPED_GRACE_MS` | `1500` | How long typing capture waits for a dictation that may still be committing |
| `WISPR_WS_URL`, `WISPR_API_KEY` | (empty) | Optional Wispr WebSocket feed |

## Storage and backend

| Variable | Default | Meaning |
|---|---|---|
| `NEO4J_URI` | `bolt://localhost:7687` | Style graph database |
| `NEO4J_USER`, `NEO4J_PASSWORD` | `neo4j` / `polytone-local` | Rotated to a random password on first run by `scripts/dev.sh` |
| `POLYTONE_DATA_DIR` | `./data` | SQLite databases, logs, inbox, tokens |
| `POLYTONE_API_PORT` | `8765` | Local backend port (127.0.0.1 only) |
| `POLYTONE_API_TOKEN` | (set by the app) | Bearer token for the local API. Leave it unset. |
| `POLYTONE_DATA_KEY` | (unset) | Base64 32-byte key that overrides the Keychain. **For tests and CI only.** |

## App (Rust shell)

| Variable | Meaning |
|---|---|
| `POLYTONE_REPO` | Path to this repo, so a bundled `Polytone.app` running elsewhere can find the backend |
| `POLYTONE_START_HIDDEN=1` | Start in the menu bar only. The window still opens when permissions are missing. |

## In-app settings

These are changed in **Settings** and stored in the SQLite `config` table.

| Setting | Default | Meaning |
|---|---|---|
| Shortcut | ⌘⇧P | The rewrite hotkey. It can be paused, and pausing also stops typing capture. |
| Insert by | Paste | *Paste* restores the clipboard afterwards. *Type* types the draft key by key. |
| Draft quality | Best | *Best* uses `POLYTONE_MODEL`. *Fast* uses Claude Haiku. |
| Learn from what I type (`capture_enabled`) | Off | Typing capture in allowlisted apps (`capture_apps`) |
| Auto-lock after (`lock_idle_minutes`) | 15 min | Locks the UI after this much inactivity |
| Delete captured text after (`retention_days`) | Never | Purges typed and dictated samples and drafts older than this |
| `learning_eval_days` | 7 | How often the learning check runs (0 means manual only) |
