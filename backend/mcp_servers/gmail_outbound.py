"""Gmail OUTBOUND-only source + MCP server.

Only mail the user wrote is ever returned:
1. the Gmail search is hard-coded to ``in:sent from:me`` (callers cannot change it),
2. every fetched message must carry the ``SENT`` label, and
3. its ``From`` address must equal the authenticated account address.
Anything else (inbound, drafts, mail "from me" via another alias the API reports differently)
is dropped and counted under ``blocked``.

Bodies: text/plain preferred, else HTML stripped; quoted replies, forwarded blocks and
signatures are removed. Incremental sync: the newest ``internalDate`` seen is stored in SQLite
config (``gmail.last_internal_date``; ``gmail.history_id`` records the mailbox historyId at
the last sync) and the next run queries ``after:<epoch>``. Pagination is followed up to the
per-run cap (default 500, newest first).

Setup (one time, needs the user's Google OAuth *Desktop app* client):
    1. put the client JSON at GMAIL_CREDENTIALS_PATH (default ./credentials.json)
    2. uv sync --extra gmail
    3. uv run python -m backend.mcp_servers.gmail_outbound auth   → writes GMAIL_TOKEN_PATH
Then: ``... gmail_outbound sync`` (one-shot ingest) or no argument (MCP stdio server).
"""
from __future__ import annotations

import base64
import logging
import sys
from collections.abc import Iterator
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from backend.mcp_servers._common import (
    SourceItem,
    address_of,
    clean_email_body,
    get_state,
    html_to_text,
    make_server,
    outbound,
    set_state,
    state_db,
)

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
QUERY = "in:sent from:me"  # hard-coded: inbound mail must never be listed
DEFAULT_CAP = 500
PAGE_SIZE = 100
AUTH_CMD = "uv run python -m backend.mcp_servers.gmail_outbound auth"


# --------------------------------------------------------------------------- credentials
def google_libs_available() -> bool:
    try:
        import google.oauth2.credentials  # noqa: F401
        import googleapiclient.discovery  # noqa: F401
    except ImportError:
        return False
    return True


def _paths(settings: Any) -> tuple[Path, Path]:
    return Path(settings.gmail_credentials_path).expanduser(), Path(settings.gmail_token_path).expanduser()


def load_credentials(settings: Any) -> Any | None:
    """Stored OAuth user credentials (refreshed if expired), or None if not authorised yet."""
    _, token_path = _paths(settings)
    if not token_path.exists() or not google_libs_available():
        return None
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds.valid and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json())
    return creds if creds.valid else None


def authorize(settings: Any, port: int = 0) -> Path:
    """Run the installed-app OAuth flow in the browser and store the token."""
    creds_path, token_path = _paths(settings)
    if not creds_path.exists():
        raise SystemExit(
            f"Gmail OAuth client not found at {creds_path}. Create a Desktop-app OAuth client in "
            "Google Cloud Console (Gmail API enabled), download the JSON there, or set GMAIL_CREDENTIALS_PATH."
        )
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
    creds = flow.run_local_server(port=port, open_browser=True)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())
    token_path.chmod(0o600)
    return token_path


def build_service(settings: Any) -> Any | None:
    creds = load_credentials(settings)
    if creds is None:
        return None
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# --------------------------------------------------------------------------- message parsing
def _b64(data: str | None) -> str:
    if not data:
        return ""
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    return raw.decode("utf-8", errors="replace")


def _walk(part: dict[str, Any]) -> Iterator[dict[str, Any]]:
    yield part
    for p in part.get("parts") or []:
        yield from _walk(p)


def extract_body(payload: dict[str, Any]) -> str:
    """Plain-text body of a Gmail ``payload`` (text/plain preferred, else stripped HTML)."""
    plain, htm = [], []
    for p in _walk(payload or {}):
        if p.get("filename") or (p.get("body") or {}).get("attachmentId"):
            continue
        mime = (p.get("mimeType") or "").lower()
        if mime == "text/plain":
            plain.append(_b64((p.get("body") or {}).get("data")))
        elif mime == "text/html":
            htm.append(_b64((p.get("body") or {}).get("data")))
    if any(t.strip() for t in plain):
        return "\n".join(plain)
    return "\n".join(html_to_text(h) for h in htm)


def headers_of(payload: dict[str, Any]) -> dict[str, str]:
    return {h.get("name", "").lower(): h.get("value", "") for h in (payload or {}).get("headers") or []}


def message_to_item(msg: dict[str, Any], my_address: str) -> tuple[SourceItem | None, str | None]:
    """``(item, None)`` for a verified outbound message, else ``(None, reason)``."""
    labels = set(msg.get("labelIds") or [])
    payload = msg.get("payload") or {}
    hdr = headers_of(payload)
    if "SENT" not in labels:
        return None, "blocked: not labelled SENT"
    if "DRAFT" in labels:
        return None, "blocked: draft"
    if not my_address or address_of(hdr.get("from")) != my_address.lower():
        return None, "blocked: From is not the authenticated address"
    text = clean_email_body(extract_body(payload))
    if not text:
        return None, "empty body after removing quotes/signature"
    date = hdr.get("date")
    try:
        date = parsedate_to_datetime(date).isoformat() if date else None
    except (TypeError, ValueError):
        pass
    prov = outbound(
        "gmail", label="SENT", from_me=True, message_id=hdr.get("message-id") or msg.get("id"),
        gmail_id=msg.get("id"), thread_id=msg.get("threadId"), date=date,
        internal_date=int(msg.get("internalDate") or 0),
    )
    return {"text": text, "provenance": prov}, None


# --------------------------------------------------------------------------- sync
def fetch_sent(service: Any, db: Any, max_messages: int = DEFAULT_CAP) -> dict[str, Any]:
    """Fetch new SENT messages. Returns ``{"items", "fetched", "blocked", "reasons", "address"}``
    and advances the incremental cursor in SQLite config."""
    max_messages = max(1, min(int(max_messages or DEFAULT_CAP), 5000))
    profile = service.users().getProfile(userId="me").execute()
    address = (profile.get("emailAddress") or "").lower()
    last = int(get_state(db, "gmail.last_internal_date", "0") or 0)
    q = QUERY + (f" after:{last // 1000}" if last else "")

    ids: list[str] = []
    token: str | None = None
    while len(ids) < max_messages:
        req: dict[str, Any] = {"userId": "me", "q": q, "maxResults": min(PAGE_SIZE, max_messages - len(ids))}
        if token:
            req["pageToken"] = token
        resp = service.users().messages().list(**req).execute()
        ids.extend(m["id"] for m in resp.get("messages") or [])
        token = resp.get("nextPageToken")
        if not token:
            break

    items: list[SourceItem] = []
    reasons: dict[str, int] = {}
    blocked = 0
    newest = last
    for mid in ids[:max_messages]:
        msg = service.users().messages().get(userId="me", id=mid, format="full").execute()
        internal = int(msg.get("internalDate") or 0)
        if last and internal <= last:  # `after:` has day granularity; skip what we already saw
            continue
        item, reason = message_to_item(msg, address)
        if item is None:
            if reason and reason.startswith("blocked"):
                blocked += 1
            reasons[reason or "skipped"] = reasons.get(reason or "skipped", 0) + 1
            continue
        newest = max(newest, internal)
        items.append(item)

    set_state(
        db, **{"gmail.address": address, "gmail.history_id": str(profile.get("historyId") or ""),
               "gmail.last_internal_date": str(newest)}
    )
    return {"items": items, "fetched": len(ids), "blocked": blocked, "reasons": reasons, "address": address}


# --------------------------------------------------------------------------- source hook
def is_enabled(settings: Any) -> bool:
    return google_libs_available() and _paths(settings)[1].exists()


def status(settings: Any) -> str:
    if not google_libs_available():
        return "needs setup: install the gmail extra (uv sync --extra gmail)"
    creds_path, token_path = _paths(settings)
    if not token_path.exists():
        if not creds_path.exists():
            return f"needs credentials: OAuth client JSON missing at {creds_path}; then run `{AUTH_CMD}`"
        return f"needs credentials: run `{AUTH_CMD}` to authorise read-only Gmail access"
    db = state_db(settings)
    addr = get_state(db, "gmail.address")
    return f"ready ({addr})" if addr else "ready"


def collect(settings: Any, max_messages: int = DEFAULT_CAP, service: Any = None) -> list[SourceItem]:
    service = service or build_service(settings)
    if service is None:
        log.info("gmail: %s", status(settings))
        return []
    return fetch_sent(service, state_db(settings), max_messages)["items"]


# --------------------------------------------------------------------------- MCP server
mcp = make_server(
    "polytone-gmail-outbound",
    "Ingests ONLY the user's own sent Gmail (in:sent from:me, SENT label, From == account). Read-only scope.",
)


def sync_sent_impl(settings: Any = None, max_messages: int = DEFAULT_CAP, service: Any = None, runtime: Any = None):
    from backend.langgraph.ingest_graph import run_ingest_batch
    from backend.runtime import get_runtime

    rt = runtime or get_runtime()
    settings = settings or rt.settings
    service = service or build_service(settings)
    if service is None:
        return {"ok": False, "status": status(settings)}
    got = fetch_sent(service, rt.db, max_messages)
    res = run_ingest_batch(got["items"], "gmail", runtime=rt)
    return {"ok": True, "fetched": got["fetched"], "blocked": got["blocked"], "skipped": got["reasons"], **res}


@mcp.tool()
def sync_sent(max_messages: int = DEFAULT_CAP) -> dict[str, Any]:
    """Fetch new messages from Sent (outbound only) and ingest them through the firewall."""
    return sync_sent_impl(max_messages=max_messages)


@mcp.tool(name="status")
def status_tool() -> dict[str, Any]:
    """Whether Gmail is authorised, the account address and the incremental cursor."""
    from backend.runtime import get_runtime

    rt = get_runtime()
    cfg = rt.db.get_config()
    return {
        "enabled": is_enabled(rt.settings), "status": status(rt.settings), "query": QUERY, "scopes": SCOPES,
        "address": cfg.get("gmail.address"), "last_internal_date": cfg.get("gmail.last_internal_date"),
        "history_id": cfg.get("gmail.history_id"), "items_ingested": rt.db.count_samples(source="gmail"),
    }


def main(argv: list[str] | None = None) -> None:
    import json

    from backend.config import get_settings

    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "serve"
    if cmd == "auth":
        path = authorize(get_settings())
        print(f"Gmail authorised (scope gmail.readonly). Token saved to {path}")
    elif cmd == "status":
        print(status(get_settings()))
    elif cmd == "sync":
        cap = int(argv[1]) if len(argv) > 1 else DEFAULT_CAP
        print(json.dumps(sync_sent_impl(max_messages=cap), indent=2))
    elif cmd == "serve":
        mcp.run("stdio")
    else:
        raise SystemExit("usage: python -m backend.mcp_servers.gmail_outbound [serve|auth|sync [N]|status]")


if __name__ == "__main__":
    main()
