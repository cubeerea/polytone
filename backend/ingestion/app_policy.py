"""Which apps Polytone may learn from: one list (``capture_apps`` in ``/config``) for both typing
capture and Wispr Flow dictation.

Wispr Flow records the app each dictation went into. Current versions store the macOS bundle id
(``com.google.Chrome``), the same id the typing list holds; older versions stored a display name
(``Slack``), which is matched through ``DISPLAY_NAMES``. A dictation with no recorded app is never
learned: Polytone can't tell whether it went into an app the user allowed.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

# bundle id → the display name older Wispr Flow versions recorded instead. Only distinctive names:
# a generic one ("Code", "Claude") could let an unrelated app match a listed one.
DISPLAY_NAMES: dict[str, str] = {
    "com.tinyspeck.slackmacgap": "Slack",
    "com.apple.MobileSMS": "Messages",
    "com.apple.mail": "Mail",
    "com.apple.Notes": "Notes",
    "com.apple.TextEdit": "TextEdit",
    "com.hnc.Discord": "Discord",
    "net.whatsapp.WhatsApp": "WhatsApp",
    "ru.keepcoder.Telegram": "Telegram",
    "com.google.Chrome": "Google Chrome",
    "com.apple.Safari": "Safari",
    "company.thebrowser.Browser": "Arc",
    "notion.id": "Notion",
}

NO_APP = "dictation has no recorded app"
NOT_LISTED = "app is not in the Learning app list"


def _keys(apps: Iterable[str]) -> set[str]:
    out: set[str] = set()
    for a in apps:
        a = str(a).strip()
        if a:
            out.add(a.casefold())
            name = DISPLAY_NAMES.get(a)
            if name:
                out.add(name.casefold())
    return out


def skip_reason(app: str | None, apps: Iterable[str]) -> str | None:
    """None when ``app`` (a bundle id or an old-style display name) is in ``apps``, else why not."""
    app = (app or "").strip()
    if not app:
        return NO_APP
    return None if app.casefold() in _keys(apps) else NOT_LISTED


def allowed_apps(db: Any) -> list[str]:
    """The Learning app list from ``db`` (a ``Database``). Fails closed: [] when the stored value
    can't be read or isn't a JSON list — unlike ``Database.capture_config``, which falls back to
    the default list (that list includes browsers, so falling back would widen what is learned)."""
    try:
        apps = json.loads(db.get_config()["capture_apps"])
    except Exception:  # noqa: BLE001 - no readable list → learn from no app
        return []
    if not isinstance(apps, list):
        return []
    return [str(a).strip() for a in apps if isinstance(a, str) and a.strip()]
