"""Latency features: pooled-connection keep-alive, the hotkey-time API warm-up (POST /warm), the
"fast" draft speed, and the startup example-cache warm-up. No network: the SDK client is faked."""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from backend.langgraph import examples
from backend.langgraph.generate_graph import run_generation, warm
from backend.llm import client as llm_client
from backend.llm.client import FAST_MODEL, KEEPALIVE_S, AnthropicLLM, MockLLM
from backend.security.tls import httpx_client

from .conftest import HUMAN_SAMPLES, api_client


class FakeSDK:
    """Just enough of ``anthropic.Anthropic`` for complete() and warm()."""

    def __init__(self, fail_warm: bool = False):
        self.created: list[dict] = []
        self.retrieved: list[str] = []
        self.fail_warm = fail_warm
        self.warm_started = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.messages = SimpleNamespace(create=self._create)
        self.models = SimpleNamespace(retrieve=self._retrieve)

    def with_options(self, **_kw):
        return self

    def _create(self, **kw):
        self.created.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")], stop_reason="end_turn", usage=None)

    def _retrieve(self, model):
        self.warm_started.set()
        self.release.wait(5)
        self.retrieved.append(model)
        if self.fail_warm:
            raise ConnectionError("offline")
        return SimpleNamespace(id=model)


# ---------------------------------------------------------------- connection pool
def test_real_client_keeps_idle_connections_alive():
    assert KEEPALIVE_S >= 60
    c = httpx_client(timeout=5, keepalive_expiry=KEEPALIVE_S)
    assert c._transport._pool._keepalive_expiry == KEEPALIVE_S
    llm = AnthropicLLM("sk-test", "claude-sonnet-5-5")  # the production construction path
    assert llm._client._client._transport._pool._keepalive_expiry == KEEPALIVE_S


# ---------------------------------------------------------------- warm()
def test_warm_opens_a_connection_once_and_skips_while_fresh(monkeypatch):
    sdk = FakeSDK()
    llm = AnthropicLLM("k", "claude-sonnet-5-5", client=sdk)
    assert llm.warm(block=True) is True and sdk.retrieved == ["claude-sonnet-5-5"]
    assert llm.warm(block=True) is False  # talked to the API < WARM_FRESH_S ago
    t = [1000.0]
    monkeypatch.setattr(llm_client.time, "time", lambda: t[0])
    llm._last_io = t[0] - llm_client.WARM_FRESH_S - 1
    assert llm.warm(block=True) is True and len(sdk.retrieved) == 2


def test_a_completed_draft_counts_as_fresh_io():
    sdk = FakeSDK()
    llm = AnthropicLLM("k", client=sdk)
    llm.complete("S", "U")
    assert llm.warm(block=True) is False and sdk.retrieved == []


def test_warm_is_non_blocking_and_single_flight():
    sdk = FakeSDK()
    sdk.release.clear()
    llm = AnthropicLLM("k", client=sdk)
    assert llm.warm() is True  # returns at once; the request runs on a thread
    assert sdk.warm_started.wait(2)
    assert llm.warm() is False  # one already in flight
    sdk.release.set()
    for _ in range(200):
        if not llm._warming:
            break
        threading.Event().wait(0.01)
    assert sdk.retrieved == ["claude-sonnet-5-5"] and not llm._warming


def test_warm_failure_is_swallowed_and_retried_next_time():
    sdk = FakeSDK(fail_warm=True)
    llm = AnthropicLLM("k", client=sdk)
    assert llm.warm(block=True) is True  # no exception
    assert llm.warm(block=True) is True  # failure did not mark the connection fresh
    assert len(sdk.retrieved) == 2


def test_mock_llm_warm_is_a_no_op():
    assert MockLLM().warm() is False


# ---------------------------------------------------------------- per-draft model
def test_complete_uses_the_override_model():
    sdk = FakeSDK()
    llm = AnthropicLLM("k", "claude-sonnet-5-5", client=sdk)
    llm.complete("S", "U")
    llm.complete("S", "U", model=FAST_MODEL)
    assert [c["model"] for c in sdk.created] == ["claude-sonnet-5-5", FAST_MODEL]
    assert all(c["output_config"] == {"effort": "low"} for c in sdk.created)


def test_draft_speed_fast_routes_generation_to_the_fast_model(runtime):
    llm = runtime.llm
    assert isinstance(llm, MockLLM)
    run_generation("tell sam the meeting moved to 3", runtime=runtime)
    runtime.db.set_config(draft_speed="fast")
    run_generation("ask dana for the deck", runtime=runtime)
    runtime.db.set_config(draft_speed="quality")
    run_generation("thank priya for the notes", runtime=runtime)
    assert llm.models == [None, FAST_MODEL, None]


def test_plain_two_argument_llms_still_work_in_quality_mode(runtime):
    class Plain:
        name = "plain"

        def complete(self, system, user):
            return "fine by me"

    runtime.llm = Plain()
    assert run_generation("say yes", runtime=runtime)["text"] == "fine by me"


def test_config_api_draft_speed(client):
    assert client.get("/config").json()["draft_speed"] == "quality"
    assert client.put("/config", json={"draft_speed": "fast"}).json()["draft_speed"] == "fast"
    assert client.put("/config", json={"draft_speed": "turbo"}).status_code == 422
    assert client.put("/config", json={"hotkey": "CmdOrCtrl+Shift+P"}).json()["draft_speed"] == "fast"  # kept


# ---------------------------------------------------------------- POST /warm
def test_warm_endpoint(runtime):
    calls = []

    class Warmable(MockLLM):
        def warm(self, *, block=False):
            calls.append(block)
            return True

    runtime.llm = Warmable()
    with api_client(runtime) as c:
        assert c.post("/warm").json() == {"ok": True, "started": True}
    with api_client(runtime, token=None) as anon:
        assert anon.post("/warm").status_code == 401
    assert calls and all(b is False for b in calls)  # never blocks a request (startup warms too)


def test_warm_endpoint_with_mock_llm(client):
    assert client.post("/warm").json() == {"ok": True, "started": False}


# ---------------------------------------------------------------- startup warm-up
def test_startup_warm_primes_examples_and_the_connection(runtime):
    for t in HUMAN_SAMPLES:
        client_resp = run_ingest_text(runtime, t)
        assert client_resp["accepted"] == 1
    examples._cache.clear()
    examples._picks.clear()
    warmed = []
    runtime.llm.warm = lambda **kw: warmed.append(kw) or True
    warm(runtime)
    assert warmed == [{}]
    assert len(examples._cache) > 0  # the first draft's excerpt split is already done


def run_ingest_text(runtime, text):
    from backend.langgraph.ingest_graph import run_ingest

    return run_ingest(text, "manual", "Default", {"source": "manual"}, runtime=runtime)


@pytest.fixture(autouse=True)
def _restore_examples_cache():
    yield
    examples._cache.clear()
    examples._picks.clear()


# ---------------------------------------------------------------- dropped connections
def _req():
    import httpx2

    return httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


class FlakySDK(FakeSDK):
    def __init__(self, errors):
        super().__init__()
        self.errors = list(errors)
        self.messages = SimpleNamespace(create=self._flaky)

    def _flaky(self, **kw):
        if self.errors:
            self.created.append(kw)
            raise self.errors.pop(0)
        return self._create(**kw)


def test_dropped_connection_is_retried_once():
    import anthropic

    sdk = FlakySDK([anthropic.APIConnectionError(request=_req())])
    assert AnthropicLLM("k", client=sdk).complete("S", "U") == "ok"
    assert len(sdk.created) == 2 and sdk.created[0] == sdk.created[1]


def test_only_one_reconnect_and_never_on_timeout():
    import anthropic

    twice = FlakySDK([anthropic.APIConnectionError(request=_req())] * 2)
    with pytest.raises(anthropic.APIConnectionError):
        AnthropicLLM("k", client=twice).complete("S", "U")
    assert len(twice.created) == 2
    slow = FlakySDK([anthropic.APITimeoutError(request=_req())])
    with pytest.raises(anthropic.APITimeoutError):
        AnthropicLLM("k", client=slow).complete("S", "U")
    assert len(slow.created) == 1  # the 8s budget has no room for a second attempt


def test_warm_after_the_mac_slept_reopens_the_connection(monkeypatch):
    """time.monotonic stops while macOS sleeps; the freshness check uses the wall clock."""
    sdk = FakeSDK()
    llm = AnthropicLLM("k", client=sdk)
    wall = [1_000_000.0]
    monkeypatch.setattr(llm_client.time, "time", lambda: wall[0])
    llm.complete("S", "U")
    assert llm.warm(block=True) is False
    wall[0] += 3600  # an hour asleep: monotonic barely moved, the wall clock did
    assert llm.warm(block=True) is True and sdk.retrieved == ["claude-sonnet-5-5"]
    wall[0] -= 7200  # the clock was set back: treat as stale, warm again
    assert llm.warm(block=True) is True


def test_generation_reports_the_model_it_used_for_the_audit(runtime, client):
    assert run_generation("ask dana for the deck", runtime=runtime)["model"] == "mock"
    r = client.post("/generate", json={"intent": "ask dana for the deck"}).json()
    assert "model" not in r  # the /generate response shape is unchanged
