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


@pytest.fixture
def client(runtime):
    from fastapi.testclient import TestClient

    from backend.daemon.server import create_app

    with TestClient(create_app(runtime)) as c:
        yield c
