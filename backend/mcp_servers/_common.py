"""Shared helpers for the Polytone MCP servers / ingestion sources.

- ``make_server(name, instructions)`` — FastMCP server (mcp 1.x ``FastMCP`` or its mcp 2.x
  successor ``MCPServer``; both expose ``@server.tool()`` and ``server.run("stdio")``).
- ``state_db(settings)`` — the SQLite ``Database`` holding per-source sync state (config keys).
- ``user_emails(settings)`` — the user's own addresses (SQLite config ``user_emails`` or env
  ``POLYTONE_USER_EMAILS``, comma separated; plus the authenticated Gmail address once known).
- ``html_to_text`` / ``clean_email_body`` — body extraction shared by gmail + mbox parsing.

Sources never write the graph: they return items that go through ``run_ingest`` (firewall).
"""
from __future__ import annotations

import html
import os
import re
import threading
from email.utils import getaddresses, parseaddr
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, TypedDict

from backend.storage.db import Database


class SourceItem(TypedDict, total=False):
    text: str
    provenance: dict[str, Any]
    persona: str


def outbound(source: str, **extra: Any) -> dict[str, Any]:
    """Provenance for text the user wrote (never inbound / generated)."""
    return {"source": source, "direction": "outbound", "generated": False, **extra}


# --------------------------------------------------------------------------- MCP server factory
def make_server(name: str, instructions: str | None = None) -> Any:
    try:  # mcp 1.x
        from mcp.server.fastmcp import FastMCP  # type: ignore[import-not-found]
    except ImportError:  # mcp 2.x renamed FastMCP → MCPServer (same decorator API)
        from mcp.server.mcpserver import MCPServer as FastMCP  # type: ignore[no-redef]
    return FastMCP(name, instructions=instructions)


# --------------------------------------------------------------------------- state / config
_dbs: dict[str, Database] = {}
_dbs_lock = threading.Lock()


def state_db(settings: Any) -> Database:
    """The runtime's Database when ``settings`` belongs to the active runtime, else one cached
    ``Database`` per file (opening one runs the schema script and holds per-thread connections)."""
    from backend import runtime as runtime_mod

    rt = runtime_mod._runtime
    if rt is not None and rt.settings is settings:
        return rt.db
    key = str(Path(settings.sqlite_path).resolve())
    with _dbs_lock:
        db = _dbs.get(key)
        if db is None:
            db = _dbs[key] = Database(settings.sqlite_path)
        return db


def get_state(db: Database, key: str, default: str | None = None) -> str | None:
    return db.get_config().get(key, default)


def set_state(db: Database, **values: Any) -> None:
    db.set_config({k.replace("__", "."): v for k, v in values.items()})


def user_emails(settings: Any, db: Database | None = None) -> set[str]:
    db = db or state_db(settings)
    cfg = db.get_config()
    raw = ",".join(
        x for x in (os.environ.get("POLYTONE_USER_EMAILS", ""), cfg.get("user_emails", ""), cfg.get("gmail.address", ""))
        if x
    )
    return {addr.lower() for _, addr in getaddresses([raw]) if "@" in addr}


def address_of(header: str | None) -> str:
    return parseaddr(header or "")[1].strip().lower()


# --------------------------------------------------------------------------- text cleanup
class _HTMLText(HTMLParser):
    _BLOCK = frozenset({"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0
        self._quote = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        cls = " ".join(v or "" for k, v in attrs if k == "class")
        if tag in ("script", "style", "head"):
            self._skip += 1
        elif tag in ("blockquote", "div"):
            if self._quote:
                self._quote += 1  # nested inside a quoted block
            elif tag == "blockquote" or "gmail_quote" in cls or "gmail_signature" in cls:
                self._quote = 1
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "head") and self._skip:
            self._skip -= 1
        elif tag in ("blockquote", "div") and self._quote:
            self._quote -= 1
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip and not self._quote:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    p = _HTMLText()
    try:
        p.feed(markup)
        p.close()
        text = "".join(p.parts)
    except Exception:  # noqa: BLE001 - malformed HTML → crude strip
        text = re.sub(r"<[^>]+>", " ", markup)
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


_CUT_PATTERNS = [
    re.compile(r"^\s*On .{0,300}wrote:\s*$", re.IGNORECASE),  # Gmail / Apple reply header
    re.compile(r"^\s*On .{0,200}$", re.IGNORECASE),  # (wrapped variant; checked with the next line)
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}", re.IGNORECASE),
    re.compile(r"^\s*-{2,}\s*Forwarded message\s*-{2,}", re.IGNORECASE),
    re.compile(r"^\s*Begin forwarded message:", re.IGNORECASE),
    re.compile(r"^\s*_{10,}\s*$"),  # Outlook separator
    re.compile(r"^\s*From:\s.+$", re.IGNORECASE),  # Outlook quoted header block (requires Sent:/Date: next)
    re.compile(r"^-- ?$"),  # RFC 3676 signature delimiter
    re.compile(r"^\s*Sent from my (iPhone|iPad|Android|mobile|BlackBerry|Galaxy).*$", re.IGNORECASE),
    re.compile(r"^\s*Get Outlook for (iOS|Android).*$", re.IGNORECASE),
]


def clean_email_body(text: str) -> str:
    """Strip quoted replies, forwarded blocks and signatures; keep only what the user typed."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    for i, line in enumerate(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if _CUT_PATTERNS[0].match(line):
            break
        if _CUT_PATTERNS[1].match(line) and re.search(r"wrote:\s*$", nxt, re.IGNORECASE) and not line.rstrip().endswith("."):
            break
        if any(p.match(line) for p in _CUT_PATTERNS[2:6]):
            break
        if _CUT_PATTERNS[6].match(line) and re.match(r"^\s*(Sent|Date|To):", nxt, re.IGNORECASE):
            break
        if any(p.match(line) for p in _CUT_PATTERNS[7:]):
            break
        if line.lstrip().startswith(">"):
            continue
        out.append(line.rstrip())
    body = "\n".join(out)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", body).strip()


def truthy_env(name: str, default: bool = True) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() not in ("0", "false", "no", "off")
