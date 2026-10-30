# Polytone MCP servers

Four stdio MCP servers. Each one is also a plain-Python ingestion source: the daemon's
`POST /ingest/sources/{name}/run` calls the module's `collect(settings)` through
`backend/ingestion/sources.py`, so ingestion works with or without an MCP client. Every item
goes through `run_ingest` (validate, firewall, extract, write_graph). No server writes to the
graph directly.

| Module | Source name | Tools | Needs |
|---|---|---|---|
| `graph_server` | (not a source) | `execute_read(query, params)` (read-only), `get_constraints(persona, k)`, `graph_stats()` | Neo4j (falls back to the in-memory store) |
| `gmail_outbound` | `gmail` | `sync_sent(max_messages)`, `status()` | Google OAuth Desktop client + one-time `auth` |
| `wispr` | `wispr` | `sync_history(max_items)`, `listen(seconds, max_items)`, `status()` | local Wispr Flow install, and/or `WISPR_WS_URL` + `WISPR_API_KEY` |
| `file_watcher` | `watcher` | `scan_now()`, `status()` | `POLYTONE_WATCH_DIR` (default `data/inbox`) |

Run any of them by hand: `uv run python -m backend.mcp_servers.<name>`. The servers use the
official `mcp` SDK's FastMCP API (`mcp.server.fastmcp.FastMCP` on mcp 1.x, renamed to
`mcp.server.mcpserver.MCPServer` on mcp 2.x; `_common.make_server` handles both).

## Registering with an MCP client

### Claude Code

```bash
cd /Users/hanksha/Code/polytone
claude mcp add polytone-graph   -- uv run --directory "$PWD" python -m backend.mcp_servers.graph_server
claude mcp add polytone-gmail   -- uv run --directory "$PWD" --extra gmail python -m backend.mcp_servers.gmail_outbound
claude mcp add polytone-wispr   -- uv run --directory "$PWD" python -m backend.mcp_servers.wispr
claude mcp add polytone-watcher -- uv run --directory "$PWD" python -m backend.mcp_servers.file_watcher
```

Or put it in a project `.mcp.json`:

```json
{
  "mcpServers": {
    "polytone-graph":   { "command": "uv", "args": ["run", "--directory", "/Users/hanksha/Code/polytone", "python", "-m", "backend.mcp_servers.graph_server"] },
    "polytone-gmail":   { "command": "uv", "args": ["run", "--directory", "/Users/hanksha/Code/polytone", "--extra", "gmail", "python", "-m", "backend.mcp_servers.gmail_outbound"] },
    "polytone-wispr":   { "command": "uv", "args": ["run", "--directory", "/Users/hanksha/Code/polytone", "python", "-m", "backend.mcp_servers.wispr"] },
    "polytone-watcher": { "command": "uv", "args": ["run", "--directory", "/Users/hanksha/Code/polytone", "python", "-m", "backend.mcp_servers.file_watcher"] }
  }
}
```

### Claude Desktop

Add the same `mcpServers` block to
`~/Library/Application Support/Claude/claude_desktop_config.json`. Use the absolute path to
`uv` (`which uv`) if Claude Desktop can't find it on its `PATH`. Optional `"env"` entries
(`POLYTONE_DATA_DIR`, `NEO4J_URI`, `POLYTONE_USER_EMAILS`, ...) override `.env`.

## graph_server

- `execute_read` refuses any query with a write clause (`CREATE`, `MERGE`, `DELETE`,
  `DETACH`, `SET`, `REMOVE`, `DROP`, `FOREACH`, `LOAD CSV`, `... IN TRANSACTIONS`,
  `CALL dbms.*`, `apoc.*`, `gds.*`, and any `db.*` procedure that isn't a known read). String
  literals and comments are blanked out before the check, so `{text:'set'}` is still allowed.
  Queries that pass the check then run in a Neo4j **READ** transaction, so the server rejects
  writes even if the static check misses one. At most 1000 rows are returned.
- `get_constraints(persona, k)` returns the same `Constraints` dict that `/generate` uses.
  `backend/mcp_servers/client.py` provides `get_constraints_via_mcp()`, which spawns this
  server over stdio and makes the tool call, so the PRD's "execute_read MCP tool call to retrieve
  the top-k constraints" path exists and is covered by an end-to-end test. `/generate` still
  calls the in-process store, because a process spawn plus the MCP handshake costs about 0.5-1s
  of the 5s budget. Both paths run the same `GraphStore.get_constraints` code, so they return
  the same result.
- `POLYTONE_GRAPH_STORE=memory` forces the in-memory store.

## gmail_outbound (outbound only)

1. In Google Cloud Console, enable the Gmail API and create an OAuth client of type
   **Desktop app**. Download its JSON to `GMAIL_CREDENTIALS_PATH` (default `./credentials.json`).
2. `uv sync --extra gmail`
3. `uv run python -m backend.mcp_servers.gmail_outbound auth`. This opens a browser and stores
   the token at `GMAIL_TOKEN_PATH` (default `data/gmail_token.json`, mode 600). The scope is
   `gmail.readonly`.

Inbound mail is blocked three ways. The query is hard-coded to `in:sent from:me`. Each message
must carry the `SENT` label and must not be a `DRAFT`. Its `From` address must equal the
authenticated account address. Bodies prefer `text/plain` (otherwise the HTML is stripped).
Quoted replies, forwarded blocks and signatures are removed. Sync is incremental: the newest
`internalDate` is stored as `gmail.last_internal_date` in SQLite config (`gmail.history_id` is
recorded too). Results are paginated and capped at 500 per run. Messages come newest first, so a
very large first backlog is truncated to the newest 500.
Other commands: `... gmail_outbound sync [N]`, `... gmail_outbound status`.
Until the token exists, the source reports `needs credentials: ...` and `collect()` returns `[]`.

## wispr (raw transcripts only)

- **Local history**: the source reads Wispr Flow's Electron SQLite DB
  (`~/Library/Application Support/Wispr Flow/flow.sqlite`, or `com.electron.wispr-flow`).
  It never opens the original file. It snapshots the DB (with `-wal`/`-shm`, using an APFS clone
  when possible) into a temp dir and opens the copy `mode=ro`. The schema is inspected at run
  time: on current versions it picks table `History`, column `asrText` (raw ASR), and ignores
  `formattedText`, `editedText`, `toneMatchedText`, `pastedText` and the other polished columns.
  Dismissed and archived rows are skipped. The run is incremental by `timestamp`
  (`wispr.history_last_ts`). Override the search dirs with `WISPR_HISTORY_DIRS`, or turn this
  path off with `POLYTONE_WISPR_LOCAL=0`.
- **WebSocket**: no public Wispr developer WebSocket API could be verified, so the client is
  configurable. It needs `WISPR_WS_URL` and `WISPR_API_KEY` (sent as `Authorization: Bearer`
  and `X-API-Key`). It reconnects with exponential backoff and reads tolerant JSON field names
  (`raw_text`, `rawText`, `asr_text`, `asrText`, `raw_transcript`, `transcript`, `text`, also
  nested under `data`, `payload` or `result`). Any formatted or polished field is ignored, and
  interim results are dropped. `collect()` listens for `WISPR_WS_COLLECT_SECONDS` (default 3).
  `uv run python -m backend.mcp_servers.wispr listen` streams continuously into `run_ingest`.
- Every item gets provenance `{"source": "wispr", "raw_transcript": true, ...}`.

## file_watcher

Drop files into `POLYTONE_WATCH_DIR`:

- `.txt` / `.md`: the whole file is one sample. Files over 4000 characters are split on blank
  lines.
- `.mbox`: only messages whose `From` is one of your addresses are kept. Addresses come from
  `POLYTONE_USER_EMAILS` (comma separated), the SQLite config key `user_emails`, or the
  authenticated Gmail address. Inbound messages are skipped. If no address is configured, the
  file is skipped and retried on the next scan.
- SQLite dumps: iMessage `chat.db` (`message.text` where `is_from_me = 1`), or a generic table
  with a text column plus an `is_from_me`-style flag or a sender column. Anything else is skipped
  with a reason.

Processed files are tracked by sha256 in the `watcher_files` table, so re-runs are idempotent.
`... file_watcher scan` does a one-shot scan and `... file_watcher watch` runs continuously.
The daemon (`uv run polytone-api`) starts a watcher thread on startup when the directory exists
and `POLYTONE_WATCHER` isn't `0`, and stops it on shutdown.
