#!/usr/bin/env bash
# Rotate the Neo4j password of the running polytone-neo4j container.
#
#   bash scripts/rotate-neo4j-password.sh
#
# 1. reads the current NEO4J_PASSWORD from .env (default: polytone-local),
# 2. generates a random 32-character password,
# 3. applies it in the running container (`ALTER CURRENT USER ... ` on the system database,
#    via cypher-shell inside `docker compose exec`; the passwords go over stdin / env, not argv),
# 4. only after that succeeded, rewrites NEO4J_PASSWORD in .env (atomically, mode 0600).
# Restart the backend afterwards so it reconnects with the new password.
#
# Env overrides (tests): POLYTONE_ENV_FILE (default <repo>/.env), POLYTONE_COMPOSE_DIR (default <repo>),
# NEO4J_SERVICE (default neo4j), NEO4J_USER (default neo4j).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${POLYTONE_ENV_FILE:-$REPO/.env}"
COMPOSE_DIR="${POLYTONE_COMPOSE_DIR:-$REPO}"
SERVICE="${NEO4J_SERVICE:-neo4j}"
DB_USER="${NEO4J_USER:-neo4j}"

err() { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; }
die() { err "$*"; exit 1; }
say() { printf '\033[1m▸ %s\033[0m\n' "$*"; }

command -v docker >/dev/null || die "docker not found on PATH"
[ -f "$ENV_FILE" ] || die "$ENV_FILE not found (copy .env.example to .env first)"

# current password: last NEO4J_PASSWORD= line, surrounding quotes stripped
OLD="$(sed -n 's/^[[:space:]]*NEO4J_PASSWORD[[:space:]]*=[[:space:]]*//p' "$ENV_FILE" | tail -n 1)"
OLD="${OLD%\"}"; OLD="${OLD#\"}"; OLD="${OLD%\'}"; OLD="${OLD#\'}"
OLD="${OLD:-polytone-local}"
case "$OLD" in *"'"*|*"\\"*) die "the current password contains a quote or backslash; rotate it by hand" ;; esac

NEW="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32 || true)"
[ "${#NEW}" -eq 32 ] || die "could not generate a random password"

# Keep the new password on disk (0600) until .env holds it: if Neo4j applies the change but we
# never hear back (exec dies after the commit, Ctrl-C, a crash before the rename), the password
# is recoverable instead of unknown.
PENDING="$ENV_FILE.neo4j-password-pending"
[ ! -e "$PENDING" ] || die "$PENDING exists: an earlier rotation's outcome is unknown. If Neo4j accepts
  the password in it, put it in $ENV_FILE as NEO4J_PASSWORD; then delete the file and run this again."
(umask 077; printf '%s\n' "$NEW" >"$PENDING")
chmod 600 "$PENDING"

say "changing the Neo4j password in the running '$SERVICE' container"
# Statement on stdin and the old password in the environment (-e NAME without a value copies it
# from this process), so neither password shows up in `ps` on the host.
if ! out="$(cd "$COMPOSE_DIR" && printf "ALTER CURRENT USER SET PASSWORD FROM '%s' TO '%s';\n" "$OLD" "$NEW" |
      NEO4J_PASSWORD="$OLD" docker compose exec -T -e NEO4J_PASSWORD "$SERVICE" \
        cypher-shell -u "$DB_USER" -d system --non-interactive 2>&1)"; then
  err "Neo4j rejected the change; .env was NOT modified."
  err "(if Neo4j did apply it anyway, the new password is in $PENDING)"
  printf '%s\n' "$out" | sed 's/^/    /' >&2
  case "$out" in
    *"is not running"*|*"No such service"*|*"no container"*) err "is the container up? try: docker compose up -d --wait" ;;
    *nauthorized*|*"credentials"*) err "the password in $ENV_FILE does not match the container's current one" ;;
  esac
  exit 1
fi

# .env: replace (or append) NEO4J_PASSWORD, write a 0600 temp file next to it, then rename over
tmp="$(umask 077; mktemp "$ENV_FILE.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
if grep -q '^[[:space:]]*NEO4J_PASSWORD[[:space:]]*=' "$ENV_FILE"; then
  awk -v pw="$NEW" '/^[[:space:]]*NEO4J_PASSWORD[[:space:]]*=/ { print "NEO4J_PASSWORD=" pw; next } { print }' \
    "$ENV_FILE" >"$tmp"
else
  { cat "$ENV_FILE"; [ -z "$(tail -c 1 "$ENV_FILE")" ] || echo; echo "NEO4J_PASSWORD=$NEW"; } >"$tmp"
fi
chmod 600 "$tmp"
if ! mv -f "$tmp" "$ENV_FILE"; then
  err "Neo4j now uses a NEW password but $ENV_FILE could not be updated."
  err "Set NEO4J_PASSWORD to the password in $PENDING in $ENV_FILE by hand."
  exit 1
fi
trap - EXIT
chmod 600 "$ENV_FILE"
rm -f "$PENDING"
say "Neo4j password rotated; $ENV_FILE updated (0600). Restart the backend to use it."
