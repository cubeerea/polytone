"""Shared fixtures. Tests always run offline: mock LLM, no embeddings, temp SQLite."""
from __future__ import annotations

import os

os.environ["POLYTONE_LLM"] = "mock"
os.environ["POLYTONE_EMBEDDINGS"] = "none"
os.environ["ANTHROPIC_API_KEY"] = ""
# never read the real Wispr Flow install: Wispr tests point WISPR_HISTORY_DIRS at a temp copy
os.environ["WISPR_HISTORY_DIRS"] = os.path.join(os.sep, "nonexistent", "polytone-tests", "wispr")
os.environ.setdefault("WISPR_TYPED_GRACE_MS", "0")
# fixed test data key: tests never read or create the real Keychain item
os.environ["POLYTONE_DATA_KEY"] = "dGVzdC1rZXktdGVzdC1rZXktdGVzdC1rZXktdGVzdCE="

import pytest

from backend.config import Settings, get_settings
from backend.graph.store import InMemoryGraphStore
from backend.llm.client import MockLLM
from backend.runtime import build_runtime, set_runtime
from backend.storage.db import Database

get_settings.cache_clear()

TEST_APP_TOKEN = "test-app-token-0123456789abcdefghijklmnopqrstuv"

HUMAN_SAMPLES = [
    "hey — i don't think we can make it today. let's push to friday! thanks so much.",
    "honestly it's fine. i'll send the deck later — no rush.",
    "so the build broke again... i'm looking into it now. should be fixed by lunch.",
]


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        polytone_data_dir=tmp_path,
        polytone_llm="mock",
        polytone_embeddings="none",
        anthropic_api_key="",
        polytone_watch_dir=tmp_path / "inbox",
    )


@pytest.fixture
def db(tmp_path) -> Database:
    return Database(tmp_path / "test.sqlite3")


@pytest.fixture
def runtime(settings, tmp_path):
    rt = build_runtime(
        settings, db_path=tmp_path / "rt.sqlite3", store=InMemoryGraphStore(), llm=MockLLM(), scorer=False
    )
    set_runtime(rt)
    yield rt
    set_runtime(None)
    rt.close()


@pytest.fixture(autouse=True)
def _no_keychain(monkeypatch):
    """Tests never read or write the real Keychain (POLYTONE_DATA_KEY covers the data key)."""
    import keyring

    def refuse(*a, **k):
        raise AssertionError("tests must not touch the real Keychain")

    for name in ("get_password", "set_password", "delete_password"):
        monkeypatch.setattr(keyring, name, refuse)


def api_client(runtime, *, token: str | None = TEST_APP_TOKEN, **kw):
    """A TestClient for ``create_app(runtime)`` that talks to 127.0.0.1:<port> with the app token."""
    from fastapi.testclient import TestClient

    from backend.daemon.server import create_app

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return TestClient(
        create_app(runtime, api_token=TEST_APP_TOKEN),
        base_url=f"http://127.0.0.1:{runtime.settings.polytone_api_port}",
        headers=headers,
        **kw,
    )


@pytest.fixture
def client(runtime):
    with api_client(runtime) as c:
        yield c
