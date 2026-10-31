"""LLM clients: ``LLM`` protocol, ``AnthropicLLM`` (real), ``MockLLM`` (deterministic, offline).

``complete(system, user)`` takes the system prompt as one string, or — for clients that set
``cache_system = True`` — as ``(stable, volatile)`` parts (``prompts.build_system_parts``).

AnthropicLLM latency notes:
- one ``anthropic.Anthropic`` client per process (built once with the Runtime), so the HTTPS
  connection pool is reused across requests;
- prompt caching: the stable part carries ``cache_control`` (the style rules only change on
  ingest); the volatile part (few-shot examples, app hint) follows the breakpoint. Prefixes
  under the model's minimum (512 tokens on claude-sonnet-5-5) silently don't cache — harmless;
- ``output_config.effort`` defaults to ``low`` (``POLYTONE_EFFORT``): at ``medium`` and above the
  model thinks before almost every reply, which adds seconds before the first token — too slow
  for a hotkey rewrite under an 8s budget;
- a single 6.5s attempt, no retries (Rust gives up at 8s);
- the SDK's httpx client uses a TLS 1.3-only ``ssl.SSLContext`` (``security/tls.py``).
"""
from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 2048  # headroom for (short) adaptive thinking; drafts themselves are short
# Rust gives up at 8s; one 6.5s attempt keeps the whole loop inside that budget.
DEFAULT_TIMEOUT_S = 6.5
DEFAULT_MAX_RETRIES = 0
DEFAULT_EFFORT = "low"
EFFORTS = ("low", "medium", "high", "xhigh", "max")

System = str | Sequence[str]


@runtime_checkable
class LLM(Protocol):
    name: str  # "anthropic" | "mock"

    def complete(self, system: str, user: str) -> str: ...


def join_system(system: System) -> str:
    return system if isinstance(system, str) else "\n\n".join(p for p in system if p)


class LLMRefusal(RuntimeError):
    """The model declined (``stop_reason == "refusal"``)."""


class AnthropicLLM:
    name = "anthropic"
    cache_system = True  # accepts (stable, volatile) system parts

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-5-5",
        *,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout: float = DEFAULT_TIMEOUT_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        effort: str | None = DEFAULT_EFFORT,
        client: Any = None,
    ):
        if client is None:
            import anthropic

            from backend.security.tls import httpx_client

            # TLS 1.3 minimum (CONTRACTS.md → Network); the SDK's timeout / retries still apply
            client = anthropic.Anthropic(
                api_key=api_key, timeout=timeout, max_retries=max_retries,
                http_client=httpx_client(timeout=timeout),
            )
        self._client = client
        self.model = model
        self.max_tokens = max_tokens
        self.effort = effort if effort in EFFORTS else None

    @staticmethod
    def _system_param(system: System) -> Any:
        if isinstance(system, str):
            return system
        stable, *rest = [p for p in system if p] or [""]
        blocks: list[dict[str, Any]] = [{"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}}]
        blocks += [{"type": "text", "text": p} for p in rest]
        return blocks

    def complete(self, system: System, user: str) -> str:
        kwargs: dict[str, Any] = {}
        if self.effort:
            kwargs["output_config"] = {"effort": self.effort}
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=self._system_param(system),
            messages=[{"role": "user", "content": user}],
            **kwargs,
        )
        if getattr(resp, "stop_reason", None) == "refusal":
            details = getattr(resp, "stop_details", None)
            raise LLMRefusal(f"the model declined (category: {getattr(details, 'category', None) or 'unknown'})")
        usage = getattr(resp, "usage", None)
        if usage is not None:
            log.debug(
                "llm usage: input=%s cache_read=%s cache_write=%s output=%s",
                getattr(usage, "input_tokens", None), getattr(usage, "cache_read_input_tokens", None),
                getattr(usage, "cache_creation_input_tokens", None), getattr(usage, "output_tokens", None),
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

    def complete(self, system: System, user: str) -> str:
        system = join_system(system)
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
        return AnthropicLLM(
            settings.anthropic_api_key, settings.polytone_model,
            effort=(getattr(settings, "polytone_effort", DEFAULT_EFFORT) or "").strip().lower() or None,
        )
    if settings.llm_backend == "anthropic":
        log.warning("POLYTONE_LLM=anthropic but ANTHROPIC_API_KEY is empty; using MockLLM")
    return MockLLM()
