from types import SimpleNamespace

import pytest

from backend.config import Settings
from backend.llm.client import (
    DEFAULT_MAX_TOKENS,
    AnthropicLLM,
    LLMRefusal,
    MockLLM,
    get_llm,
)


class FakeMessages:
    def __init__(self, stop_reason="end_turn"):
        self.kwargs = None
        self.stop_reason = stop_reason

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text="  hi sam  ")],
            stop_reason=self.stop_reason,
            stop_details=SimpleNamespace(category="cyber") if self.stop_reason == "refusal" else None,
            usage=SimpleNamespace(input_tokens=10, cache_read_input_tokens=600, output_tokens=5),
        )


def test_anthropic_call_shape():
    fake = SimpleNamespace(messages=FakeMessages())
    llm = AnthropicLLM("k", "claude-sonnet-5-5", client=fake)
    assert llm.complete("SYS", "USER") == "hi sam"
    kw = fake.messages.kwargs
    assert kw["model"] == "claude-sonnet-5-5" and kw["max_tokens"] == DEFAULT_MAX_TOKENS
    assert kw["system"] == "SYS" and kw["messages"] == [{"role": "user", "content": "USER"}]
    assert kw["output_config"] == {"effort": "low"}  # latency: no long thinking before a rewrite


def test_anthropic_prompt_caching_blocks():
    fake = SimpleNamespace(messages=FakeMessages())
    llm = AnthropicLLM("k", client=fake)
    assert llm.cache_system is True
    llm.complete(("STABLE", "EXAMPLES"), "USER")
    assert fake.messages.kwargs["system"] == [
        {"type": "text", "text": "STABLE", "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "EXAMPLES"},
    ]
    llm.complete(("STABLE", ""), "USER")  # no volatile part → one cached block
    assert fake.messages.kwargs["system"] == [
        {"type": "text", "text": "STABLE", "cache_control": {"type": "ephemeral"}}
    ]
    AnthropicLLM("k", client=fake, effort="").complete("S", "U")
    assert "output_config" not in fake.messages.kwargs


def test_anthropic_refusal_raises():
    llm = AnthropicLLM("k", client=SimpleNamespace(messages=FakeMessages("refusal")))
    with pytest.raises(LLMRefusal, match="cyber"):
        llm.complete("S", "U")


def test_mock_accepts_system_parts():
    m = MockLLM()
    assert m.complete(("Casing: all lowercase", "CONTEXT: x"), "<intent>Hello Sam</intent>") == "hello sam."
    assert m.calls[-1][0] == "Casing: all lowercase\n\nCONTEXT: x"


def test_anthropic_client_config():
    llm = AnthropicLLM("sk-test", "claude-sonnet-5-5")
    assert llm._client.max_retries == 0
    assert float(llm._client.timeout) == 6.5


def test_get_llm_selection():
    assert isinstance(get_llm(Settings(polytone_llm="mock", anthropic_api_key="x")), MockLLM)
    assert isinstance(get_llm(Settings(polytone_llm="", anthropic_api_key="")), MockLLM)
    assert isinstance(get_llm(Settings(polytone_llm="anthropic", anthropic_api_key="")), MockLLM)
    real = get_llm(Settings(polytone_llm="", anthropic_api_key="sk-x", polytone_model="claude-sonnet-5-5"))
    assert isinstance(real, AnthropicLLM) and real.model == "claude-sonnet-5-5" and real.effort == "low"
    hi = get_llm(Settings(polytone_llm="anthropic", anthropic_api_key="sk-x", polytone_effort="HIGH"))
    assert hi.effort == "high"


def test_mock_is_deterministic_and_respects_hints():
    m = MockLLM()
    u = "<intent>\n- tell sam meeting moved to 3\n- ask for slides\n</intent>"
    a = m.complete("Casing: standard sentence case.", u)
    assert a == m.complete("Casing: standard sentence case.", u)
    assert a == "Sam, the meeting has moved to 3pm. Could you send over the slides?"
    assert m.complete("Casing: all lowercase", u) == a.lower()
    assert "I'm" in m.complete("Contraction rate 0.90: use contractions", "<intent>I am late</intent>")
