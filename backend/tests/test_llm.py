from types import SimpleNamespace

from backend.config import Settings
from backend.llm.client import AnthropicLLM, MockLLM, get_llm


class FakeMessages:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="  hi sam  ")])


def test_anthropic_call_shape():
    fake = SimpleNamespace(messages=FakeMessages())
    llm = AnthropicLLM("k", "claude-sonnet-5-5", client=fake)
    assert llm.complete("SYS", "USER") == "hi sam"
    kw = fake.messages.kwargs
    assert kw["model"] == "claude-sonnet-5-5" and kw["max_tokens"] == 1024
    assert kw["system"] == "SYS" and kw["messages"] == [{"role": "user", "content": "USER"}]


def test_anthropic_client_config():
    llm = AnthropicLLM("sk-test", "claude-sonnet-5-5")
    assert llm._client.max_retries == 0
    assert float(llm._client.timeout) == 6.5


def test_get_llm_selection():
    assert isinstance(get_llm(Settings(polytone_llm="mock", anthropic_api_key="x")), MockLLM)
    assert isinstance(get_llm(Settings(polytone_llm="", anthropic_api_key="")), MockLLM)
    assert isinstance(get_llm(Settings(polytone_llm="anthropic", anthropic_api_key="")), MockLLM)
    real = get_llm(Settings(polytone_llm="", anthropic_api_key="sk-x", polytone_model="claude-sonnet-5-5"))
    assert isinstance(real, AnthropicLLM) and real.model == "claude-sonnet-5-5"


def test_mock_is_deterministic_and_respects_hints():
    m = MockLLM()
    u = "<intent>\n- tell sam meeting moved to 3\n- ask for slides\n</intent>"
    a = m.complete("Casing: standard sentence case.", u)
    assert a == m.complete("Casing: standard sentence case.", u)
    assert a == "Sam, the meeting has moved to 3pm. Could you send over the slides?"
    assert m.complete("Casing: all lowercase", u) == a.lower()
    assert "I'm" in m.complete("Contraction rate 0.90: use contractions", "<intent>I am late</intent>")
