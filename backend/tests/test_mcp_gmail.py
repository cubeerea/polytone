"""Gmail outbound source against a fake Gmail API service (no network, no credentials)."""
from __future__ import annotations

import base64

from backend.ingestion import sources
from backend.mcp_servers import gmail_outbound as gm

ME = "hank@example.com"


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def _msg(mid, frm, labels, plain=None, html=None, internal=1_700_000_000_000, to="pal@example.com"):
    parts = []
    if plain is not None:
        parts.append({"mimeType": "text/plain", "body": {"data": _b64(plain)}})
    if html is not None:
        parts.append({"mimeType": "text/html", "body": {"data": _b64(html)}})
    parts.append({"mimeType": "application/pdf", "filename": "x.pdf", "body": {"attachmentId": "a1"}})
    return {
        "id": mid, "threadId": "t" + mid, "labelIds": labels, "internalDate": str(internal),
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "From", "value": frm}, {"name": "To", "value": to},
                {"name": "Date", "value": "Mon, 6 Oct 2026 10:00:00 -0700"},
                {"name": "Message-ID", "value": f"<{mid}@mail.example.com>"},
            ],
            "parts": [{"mimeType": "multipart/alternative", "parts": parts[:-1]}, parts[-1]],
        },
    }


class _Req:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class FakeGmail:
    """Mimics ``service.users().messages().list/get`` + ``getProfile`` with pagination."""

    def __init__(self, messages, page=2, address=ME):
        self.store = {m["id"]: m for m in messages}
        self.order = [m["id"] for m in messages]
        self.page = page
        self.address = address
        self.queries: list[str] = []
        self.gets: list[str] = []

    def users(self):
        return self

    def getProfile(self, userId):
        return _Req(lambda: {"emailAddress": self.address, "historyId": "4242"})

    def messages(self):
        return self

    def list(self, userId, q, maxResults, pageToken=None):
        self.queries.append(q)
        start = int(pageToken or 0)
        n = min(self.page, maxResults)
        # A real "in:sent from:me" query would filter server-side; the fake deliberately does NOT
        # so the client-side SENT/From verification is what keeps inbound out.
        ids = self.order[start:start + n]
        nxt = start + n
        out = {"messages": [{"id": i} for i in ids]}
        if nxt < len(self.order):
            out["nextPageToken"] = str(nxt)
        return _Req(lambda: out)

    def get(self, userId, id, format):
        self.gets.append(id)
        return _Req(lambda: self.store[id])


QUOTED = (
    "sounds good — i'll bring the slides tomorrow. honestly can't wait.\n\n"
    "-- \nHank\nCEO, Somewhere\n\n"
    "On Mon, Oct 6, 2026 at 9:00 AM Pal <pal@example.com> wrote:\n> can you bring the slides?\n"
)


def _mailbox():
    return [
        _msg("m1", f"Hank <{ME}>", ["SENT"], plain=QUOTED, internal=1_700_000_003_000),
        _msg("m2", "Pal <pal@example.com>", ["INBOX", "UNREAD"], plain="buy cheap watches now!!", internal=1_700_000_002_000),
        _msg("m3", f"{ME}", ["SENT", "IMPORTANT"], html="<div>quick note: we ship <b>friday</b>.</div>"
             "<div class='gmail_quote'>On Sun, Pal wrote:<blockquote>ship when?</blockquote></div>",
             internal=1_700_000_001_000),
        _msg("m4", "Spoofer <other@example.com>", ["SENT"], plain="i am not you", internal=1_700_000_000_500),
        _msg("m5", f"Hank <{ME}>", ["SENT", "DRAFT"], plain="draft text never sent", internal=1_700_000_000_400),
    ]


def test_extract_and_clean():
    item, reason = gm.message_to_item(_mailbox()[0], ME)
    assert reason is None
    assert item["text"] == "sounds good — i'll bring the slides tomorrow. honestly can't wait."
    p = item["provenance"]
    assert p["source"] == "gmail" and p["label"] == "SENT" and p["from_me"] is True
    assert p["message_id"] == "<m1@mail.example.com>" and p["date"].startswith("2026-10-06")
    assert p["direction"] == "outbound" and p["generated"] is False
    html_item, _ = gm.message_to_item(_mailbox()[2], ME)
    assert html_item["text"] == "quick note: we ship friday."


def test_inbound_and_spoofed_blocked(runtime):
    svc = FakeGmail(_mailbox())
    got = gm.fetch_sent(svc, runtime.db, max_messages=50)
    texts = [i["text"] for i in got["items"]]
    assert len(texts) == 2 and all("watches" not in t and "not you" not in t and "draft" not in t for t in texts)
    assert got["blocked"] == 3 and got["fetched"] == 5
    assert svc.queries[0] == "in:sent from:me"  # hard-coded query
    assert len(svc.queries) == 3  # paginated (page size 2)


def test_incremental_sync_and_cap(runtime):
    svc = FakeGmail(_mailbox())
    first = gm.fetch_sent(svc, runtime.db, max_messages=1)
    assert len(svc.gets) == 1 and len(first["items"]) == 1  # cap respected
    cfg = runtime.db.get_config()
    assert cfg["gmail.last_internal_date"] == "1700000003000" and cfg["gmail.history_id"] == "4242"
    assert cfg["gmail.address"] == ME
    svc2 = FakeGmail(_mailbox())
    again = gm.fetch_sent(svc2, runtime.db, max_messages=50)
    assert svc2.queries[0] == "in:sent from:me after:1700000003"
    assert again["items"] == []  # nothing newer than the cursor


def test_sync_sent_goes_through_ingest(runtime):
    res = gm.sync_sent_impl(runtime.settings, service=FakeGmail(_mailbox()), runtime=runtime)
    assert res["ok"] and res["accepted"] == 2 and res["blocked"] == 3
    samples = runtime.db.list_samples(source="gmail")
    assert len(samples) == 2
    runs = runtime.db.list_ingest_runs(source="gmail")
    assert runs and runs[0]["accepted"] == 2


def test_needs_credentials_status(runtime, tmp_path):
    s = runtime.settings
    s.gmail_credentials_path = tmp_path / "missing.json"
    s.gmail_token_path = tmp_path / "missing_token.json"
    assert gm.is_enabled(s) is False
    assert gm.status(s).startswith("needs credentials")
    assert gm.collect(s) == []
    assert gm.sync_sent_impl(s, runtime=runtime)["ok"] is False
    d = sources.describe_source("gmail", runtime)
    assert d["enabled"] is False and d["status"].startswith("needs credentials")
    r = sources.run_source("gmail", runtime)
    assert r["accepted"] == 0 and "disabled or not configured" in r["reasons"][0]
