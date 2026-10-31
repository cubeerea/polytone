"""Local API authentication. See CONTRACTS.md → Security → Local API auth.

- App token: ``POLYTONE_API_TOKEN`` if set, else 32 random bytes (base64url). Always written to
  ``<data_dir>/.api_token`` (0600, atomic replace) so Rust can attach to a running backend.
  Full scope. Compared in constant time.
- UI sessions: ``create_session()`` (``POST /session``, app token only) → an in-memory token with a
  sliding idle timeout (config ``lock_idle_minutes``) and a 12 h absolute cap. Session tokens are
  kept only as SHA-256 digests, looked up by digest (no early-exit string compare).
- ``AuthMiddleware`` (pure ASGI, so it adds ~µs, not a BaseHTTPMiddleware hop): Host must be
  ``127.0.0.1:<port>`` / ``localhost:<port>`` (DNS rebinding) else 403; ``GET /health`` is open;
  everything else needs ``Authorization: Bearer <token>`` else 401 ``{"detail":"locked"}``;
  app-only routes answer 403 to a session token. The caller kind (``"app"``/``"ui"``) is put in
  ``request.state.actor`` for the audit log.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

TOKEN_FILE = ".api_token"
ENV_TOKEN = "POLYTONE_API_TOKEN"
SESSION_CAP_S = 12 * 3600
DEFAULT_IDLE_MINUTES = 15
MIN_IDLE_MINUTES, MAX_IDLE_MINUTES = 1, 240
DENIED_AUDIT_INTERVAL_S = 60.0

OPEN_ROUTES = {("GET", "/health")}
APP_ONLY_ROUTES = {("POST", "/session"), ("DELETE", "/session"), ("POST", "/audit/event")}

Audit = Callable[[str, dict[str, Any], str], Any]


def write_token_file(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(token)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8", "replace")).digest()


@dataclass
class _Session:
    sid: str  # short id for the audit log (never the token)
    created: float
    last_seen: float


class AuthManager:
    def __init__(
        self,
        data_dir: str | Path,
        port: int,
        *,
        token: str | None = None,
        idle_minutes: int = DEFAULT_IDLE_MINUTES,
        audit: Audit | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.port = int(port)
        self.allowed_hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        self.app_token = token or os.environ.get(ENV_TOKEN, "").strip() or secrets.token_urlsafe(32)
        if len(self.app_token) < 32:
            log.warning("%s is shorter than 32 characters", ENV_TOKEN)
        self._app_digest = _digest(self.app_token)
        self.token_path = Path(data_dir) / TOKEN_FILE
        write_token_file(self.token_path, self.app_token)
        self.idle_minutes = idle_minutes
        self._audit = audit
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions: dict[bytes, _Session] = {}
        self._denied = 0
        self._denied_since = 0.0

    # ------------------------------------------------------------------ helpers
    def audit(self, event: str, detail: dict[str, Any], actor: str = "backend") -> None:
        if self._audit is not None:
            self._audit(event, detail, actor)

    @property
    def idle_timeout_s(self) -> int:
        return int(self.idle_minutes) * 60

    def set_idle_minutes(self, minutes: int) -> None:
        self.idle_minutes = max(MIN_IDLE_MINUTES, min(MAX_IDLE_MINUTES, int(minutes)))

    # ------------------------------------------------------------------ sessions
    def create_session(self) -> dict[str, Any]:
        token = secrets.token_urlsafe(32)
        now = self._clock()
        s = _Session(sid=secrets.token_hex(4), created=now, last_seen=now)
        with self._lock:
            self._sessions[_digest(token)] = s
        self.audit("session.unlock", {"session": s.sid, "idle_timeout_s": self.idle_timeout_s}, "app")
        return {
            "token": token,
            "idle_timeout_s": self.idle_timeout_s,
            "expires_at": datetime.fromtimestamp(now + SESSION_CAP_S, UTC).isoformat(timespec="seconds"),
        }

    def revoke_all(self, reason: str = "app") -> int:
        with self._lock:
            n = len(self._sessions)
            self._sessions.clear()
        self.audit("session.lock", {"sessions": n, "reason": reason}, "app")
        return n

    def revoke(self, token: str) -> bool:
        with self._lock:
            s = self._sessions.pop(_digest(token), None)
        if s is not None:
            self.audit("session.lock", {"session": s.sid, "reason": "user"}, "ui")
        return s is not None

    def active_sessions(self) -> int:
        self._sweep()
        return len(self._sessions)

    def _sweep(self) -> None:
        now = self._clock()
        idle = self.idle_timeout_s
        expired: list[tuple[_Session, str]] = []
        with self._lock:
            for d, s in list(self._sessions.items()):
                if now - s.created >= SESSION_CAP_S:
                    expired.append((s, "max_age"))
                elif now - s.last_seen >= idle:
                    expired.append((s, "idle"))
                else:
                    continue
                del self._sessions[d]
        for s, why in expired:
            self.audit("session.expired", {"session": s.sid, "reason": why}, "backend")

    # ------------------------------------------------------------------ checks
    def authenticate(self, authorization: str | None) -> tuple[str, str] | None:
        """``("app"|"ui", token)`` for a valid bearer token, else None. Slides the session."""
        if not authorization or authorization[:7].lower() != "bearer ":
            return None
        token = authorization[7:].strip()
        if not token:
            return None
        d = _digest(token)
        if hmac.compare_digest(d, self._app_digest):
            return "app", token
        self._sweep()
        with self._lock:
            s = self._sessions.get(d)
            if s is None:
                return None
            s.last_seen = self._clock()
        return "ui", token

    def host_ok(self, host: str | None) -> bool:
        return bool(host) and host.lower() in self.allowed_hosts

    def denied(self, method: str, path: str, why: str) -> None:
        """Audit ``auth.denied``, rate-limited (a locked UI polling must not flood the log)."""
        now = self._clock()
        with self._lock:
            self._denied += 1
            if now - self._denied_since < DENIED_AUDIT_INTERVAL_S:
                return
            n, self._denied, self._denied_since = self._denied, 0, now
        self.audit("auth.denied", {"method": method, "path": path[:100], "reason": why, "count": n}, "unknown")


# ---------------------------------------------------------------------- ASGI middleware
def _json(status: int, body: dict[str, Any], extra: list[tuple[bytes, bytes]] | None = None):
    payload = json.dumps(body).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]
    return status, headers + (extra or []), payload


class AuthMiddleware:
    def __init__(self, app: Any, auth: AuthManager) -> None:
        self.app = app
        self.auth = auth

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        host = authz = None
        for k, v in scope.get("headers") or ():
            if k == b"host":
                host = v.decode("latin-1")
            elif k == b"authorization":
                authz = v.decode("latin-1")
        method, path = scope["method"], scope["path"]
        if not self.auth.host_ok(host):
            self.auth.denied(method, path, "host")
            await self._reply(send, *_json(403, {"detail": "forbidden host"}))
            return
        state = scope.setdefault("state", {})
        if (method, path) in OPEN_ROUTES:
            state["actor"] = "anonymous"
            await self.app(scope, receive, send)
            return
        who = self.auth.authenticate(authz)
        if who is None:
            self.auth.denied(method, path, "token")
            await self._reply(send, *_json(401, {"detail": "locked"}, [(b"www-authenticate", b"Bearer")]))
            return
        kind, token = who
        if kind == "ui" and (method, path) in APP_ONLY_ROUTES:
            self.auth.denied(method, path, "scope")
            await self._reply(send, *_json(403, {"detail": "app token required"}))
            return
        state["actor"] = kind
        state["token"] = token
        await self.app(scope, receive, send)

    @staticmethod
    async def _reply(send: Any, status: int, headers: list, body: bytes) -> None:
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})
