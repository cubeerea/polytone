#!/usr/bin/env bash
# One-command dev launch: preflight checks, then `pnpm tauri dev`.
# Usage: npm run tauri:dev   (or: pnpm tauri:dev, bash scripts/dev.sh)
set -euo pipefail

cd "$(dirname "$0")/.."
REPO="$(pwd)"
API_PORT="${POLYTONE_API_PORT:-8765}"

say()  { printf '\033[1m▸ %s\033[0m\n' "$*"; }
warn() { printf '\033[33m! %s\033[0m\n' "$*"; }
die()  { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# GUI-less shells (and some IDE terminals) miss cargo/uv on PATH.
[ -f "$HOME/.cargo/env" ] && . "$HOME/.cargo/env"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

for bin in pnpm cargo uv; do
  command -v "$bin" >/dev/null || die "$bin not found on PATH (see README → Quick start)"
done

# 1. Dependencies (cheap no-ops when already installed).
[ -d node_modules ] || { say "installing JS deps"; pnpm install; }
[ -d .venv ] || { say "installing Python deps"; uv sync --extra embeddings --extra gmail; }
[ -f .env ] || { say "creating .env from .env.example"; cp .env.example .env; }

# 2. Neo4j. Without it the backend falls back to an empty in-memory graph.
if command -v docker >/dev/null; then
  if ! docker info >/dev/null 2>&1; then
    say "starting Docker Desktop"
    open -ga Docker || true
    for _ in $(seq 1 60); do docker info >/dev/null 2>&1 && break; sleep 1; done
  fi
  if docker info >/dev/null 2>&1; then
    docker compose up -d --wait >/dev/null 2>&1 || docker compose up -d
    say "neo4j up"
  else
    warn "Docker isn't running: the style graph will be in-memory (nothing learned is kept)"
  fi
else
  warn "docker not installed: the style graph will be in-memory"
fi

# 3. Stale processes from an earlier run of THIS repo.
#    A backend left behind keeps serving old code; a leftover vite holds port 1420.
pid_cwd() { lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p'; }
for port in 1420 "$API_PORT"; do
  for pid in $(lsof -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true); do
    if [ "$(pid_cwd "$pid")" = "$REPO" ]; then
      say "stopping stale process on :$port (pid $pid)"
      kill "$pid" 2>/dev/null || true
    else
      die "port $port is used by another program (pid $pid: $(ps -o comm= -p "$pid")). Quit it and retry."
    fi
  done
done
pkill -f "$REPO/src-tauri/target/.*/polytone" 2>/dev/null && say "stopped a running Polytone instance" || true
sleep 0.5

# 4. Launch. The app starts the backend itself and stops it on quit.
say "launching Polytone (first build takes a few minutes)"
exec pnpm tauri dev
