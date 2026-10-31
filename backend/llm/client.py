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
- the SDK's httpx client uses a TLS 1.3-only ``ssl.SSLContext`` (``security/tls.py``);
- idle connections are kept for ``KEEPALIVE_S`` (the SDK default is 5s, so a hotkey press minutes
  after the last one always paid a fresh TCP + TLS handshake, ~70ms measured), and ``warm()`` —
  called when the hotkey goes down, while Rust is still capturing the selection — re-opens the
  connection with a free ``GET /v1/models/{id}`` if it may have gone idle;
- ``complete(..., model=)`` picks the model per draft: the "fast" draft speed uses
  ``FAST_MODEL`` (Claude Haiku 5.5, ~40% lower latency measured on 80-token drafts; less literal).
- thinking stays adaptive: at ``low`` effort the model did not think on any measured draft, and
  ``thinking: between_tools`` measured no faster.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 2048  # headroom for (short) adaptive thinking; drafts themselves are short
# Rust gives up at 8s; one 6.5s attempt keeps the whole loop inside that budget.
DEFAULT_TIMEOUT_S = 6.5
DEFAULT_MAX_RETRIES = 0
DEFAULT_EFFORT = "low"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
FAST_MODEL = "claude-haiku-5-5"
# Keep idle connections to the API this long (SDK default: 5s). Measured: a pooled connection
# is still usable after 70s idle. A connection the peer dropped (sleep, network change) is
# covered by the wall-clock check in warm() and one reconnect in complete().
KEEPALIVE_S = 90.0
# warm() skips the network when the client talked to the API this recently (connection is live).
WARM_FRESH_S = 20.0
WARM_TIMEOUT_S = 3.0

System = str | Sequence[str]


@runtime_checkable
class LLM(Protocol):
    name: str  # "anthropic" | "mock"

    def complete(self, system: str, user: str, model: str | None = None) -> str: ...


def join_system(system: System) -> str:
    return system if isinstance(system, str) else "\n\n".join(p for p in system if p)


def _is_stale_connection(e: BaseException) -> bool:
    """A connection error that isn't a timeout (``APITimeoutError`` subclasses it)."""
    try:
        import anthropic
    except ImportError:  # pragma: no cover
        return False
    return isinstance(e, anthropic.APIConnectionError) and not isinstance(e, anthropic.APITimeoutError)


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
                http_client=httpx_client(timeout=timeout, keepalive_expiry=KEEPALIVE_S),
            )
        self._client = client
        self.model = model
        self.max_tokens = max_tokens
        self.effort = effort if effort in EFFORTS else None
        self._io_lock = threading.Lock()
        # WALL-clock time the API last answered: unlike time.monotonic on macOS, it keeps counting
        # while the Mac sleeps, so a press after waking always re-opens the connection
        self._last_io = float("-inf")
        self._warming = False

    def _note_io(self) -> None:
        with self._io_lock:
            self._last_io = time.time()

    def warm(self, *, block: bool = False) -> bool:
        """Make sure a pooled connection to the API is open before the next ``complete``.

        Returns at once (the request runs on a daemon thread) unless ``block``. No-op — returns
        False — while one is in flight or the client talked to the API in the last
        ``WARM_FRESH_S``. Free: ``GET /v1/models/{id}`` is not billed. Never raises."""
        with self._io_lock:
            if self._warming or 0 <= time.time() - self._last_io < WARM_FRESH_S:
                return False
            self._warming = True

        def run() -> None:
            try:
                self._client.with_options(timeout=WARM_TIMEOUT_S).models.retrieve(self.model)
                self._note_io()
            except Exception as e:  # noqa: BLE001 - best effort; the real request reconnects
                log.debug("llm warm-up failed: %s", type(e).__name__)
            finally:
                with self._io_lock:
                    self._warming = False

        if block:
            run()
        else:
            threading.Thread(target=run, name="polytone-llm-warm", daemon=True).start()
        return True

    @staticmethod
    def _system_param(system: System) -> Any:
        if isinstance(system, str):
            return system
        stable, *rest = [p for p in system if p] or [""]
        blocks: list[dict[str, Any]] = [{"type": "text", "text": stable, "cache_control": {"type": "ephemeral"}}]
        blocks += [{"type": "text", "text": p} for p in rest]
        return blocks

    def complete(self, system: System, user: str, model: str | None = None) -> str:
        kwargs: dict[str, Any] = {}
        if self.effort:
            kwargs["output_config"] = {"effort": self.effort}
        req = dict(
            model=model or self.model,
            max_tokens=self.max_tokens,
            system=self._system_param(system),
            messages=[{"role": "user", "content": user}],
            **kwargs,
        )
        try:
            resp = self._client.messages.create(**req)
        except Exception as e:
            # A pooled connection the server or network dropped while idle fails fast here
            # (reset / closed): reconnect once. Timeouts are not retried (the 8s budget).
            if not _is_stale_connection(e):
                raise
            log.info("llm connection was dropped (%s); reconnecting once", type(e).__name__)
            resp = self._client.messages.create(**req)
        self._note_io()
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
        self.models: list[str | None] = []  # the per-call model override (None = default)

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

    def warm(self, *, block: bool = False) -> bool:
        return False

    def complete(self, system: System, user: str, model: str | None = None) -> str:
        system = join_system(system)
        self.calls.append((system, user))
        self.models.append(model)
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
