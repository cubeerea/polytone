"""LLM clients: ``LLM`` protocol, ``AnthropicLLM`` (real), ``MockLLM`` (deterministic, offline)."""
from __future__ import annotations

import logging
import re
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 1024
# Rust gives up at 8s; one 6.5s attempt keeps the whole loop inside that budget.
DEFAULT_TIMEOUT_S = 6.5
DEFAULT_MAX_RETRIES = 0


@runtime_checkable
class LLM(Protocol):
    name: str  # "anthropic" | "mock"

    def complete(self, system: str, user: str) -> str: ...


class AnthropicLLM:
    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-5-5",
        *,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout: float = DEFAULT_TIMEOUT_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        client: Any = None,
    ):
        if client is None:
            import anthropic

            client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=max_retries)
        self._client = client
        self.model = model
        self.max_tokens = max_tokens

    def complete(self, system: str, user: str) -> str:
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(
            getattr(block, "text", "") for block in resp.content if getattr(block, "type", None) == "text"
        ).strip()


# ----------------------------------------------------------------------------- mock
_CONTRACT = [
    (r"\bdo not\b", "don't"), (r"\bdoes not\b", "doesn't"), (r"\bdid not\b", "didn't"),
    (r"\bis not\b", "isn't"), (r"\bare not\b", "aren't"), (r"\bwas not\b", "wasn't"),
    (r"\bcannot\b", "can't"), (r"\bcan not\b", "can't"), (r"\bwill not\b", "won't"),
    (r"\bI am\b", "I'm"), (r"\bit is\b", "it's"), (r"\bthat is\b", "that's"),
    (r"\bwe are\b", "we're"), (r"\byou are\b", "you're"), (r"\bthey are\b", "they're"),
    (r"\bI will\b", "I'll"), (r"\bwe will\b", "we'll"), (r"\bI have\b", "I've"),
]
_EXPAND = [
    (re.compile(r"\b" + re.escape(short) + r"\b", re.IGNORECASE), long_.replace(r"\b", ""))
    for long_, short in _CONTRACT
    if long_ not in (r"\bcan not\b", r"\bcannot\b")
]

_BULLET = re.compile(r"^\s*(?:[-*•·>]+|\d+[.)])\s*")


class MockLLM:
    """Deterministic stand-in. Turns the intent lines into plain sentences and honours the
    casing / contraction / exclamation hints present in the system prompt."""

    name = "mock"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    @staticmethod
    def _intent_lines(user: str) -> list[str]:
        m = re.search(r"<intent>\s*(.*?)\s*</intent>", user, re.DOTALL)
        body = m.group(1) if m else user
        lines = [_BULLET.sub("", ln).strip() for ln in body.splitlines()]
        return [ln for ln in lines if ln]

    @staticmethod
    def _sentence(line: str) -> str:
        s = line.strip().rstrip(".;,")
        low = s.lower()
        m = re.match(r"(?:tell|let)\s+(\w+)\s+(?:know\s+)?(?:that\s+)?(.+)", s, re.IGNORECASE)
        if m and low.startswith(("tell ", "let ")):
            name, rest = m.group(1), m.group(2)
            rest = re.sub(r"^(?:the\s+)?meeting\b", "the meeting", rest, flags=re.IGNORECASE)
            rest = re.sub(r"\bmoved to (\d{1,2})\b(?!:)", r"moved to \1pm", rest)
            rest = re.sub(r"^the meeting moved", "the meeting has moved", rest)
            return f"{name.capitalize()}, {rest}."
        m = re.match(r"ask (?:\w+ )?(?:for|about)\s+(.+)", s, re.IGNORECASE)
        if m:
            obj = m.group(1)
            if not obj.lower().startswith(("the ", "a ", "an ", "my ", "your ", "some ")):
                obj = "the " + obj
            return f"Could you send over {obj}?"
        m = re.match(r"ask (?:\w+ )?(?:to|if|whether)\s+(.+)", s, re.IGNORECASE)
        if m:
            return f"Could you {m.group(1)}?"
        m = re.match(r"(?:say\s+)?thanks?(?:\s+(?:for|to))?\s*(.*)", s, re.IGNORECASE)
        if m and low.startswith(("thank", "say thank")):
            return f"Thanks{(' for ' + m.group(1)) if m.group(1) else ''}!"
        if s.endswith("?"):
            return s[0].upper() + s[1:]
        return s[0].upper() + s[1:] + "."

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        sents = [self._sentence(ln) for ln in self._intent_lines(user)]
        text = " ".join(sents)
        sys_low = system.lower()
        m = re.search(r"contraction rate[^0-9]*([0-9.]+)", sys_low)
        if m:
            rate = float(m.group(1).rstrip("."))
            if rate >= 0.5:
                for pat, rep in _CONTRACT:
                    text = re.sub(pat, rep, text, flags=re.IGNORECASE)
            elif rate <= 0.2:
                for rx, rep in _EXPAND:
                    text = rx.sub(rep, text)
        if "casing: all lowercase" in sys_low:
            text = text.lower()
        if "exclamation marks: often" in sys_low:
            text = re.sub(r"\.$", "!", text)
        return text


def get_llm(settings: Any) -> LLM:
    if settings.llm_backend == "anthropic" and settings.anthropic_api_key:
        return AnthropicLLM(settings.anthropic_api_key, settings.polytone_model)
    if settings.llm_backend == "anthropic":
        log.warning("POLYTONE_LLM=anthropic but ANTHROPIC_API_KEY is empty; using MockLLM")
    return MockLLM()
