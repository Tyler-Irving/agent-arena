"""One async interface over Anthropic and Ollama.

Model strings are prefixed by provider: "anthropic/claude-sonnet-5-5",
"ollama/qwen2.5-coder:14b". Every call is written to the run log, including
failed ones.
"""

import asyncio
import os
import re
import time
from functools import partial
from typing import NamedTuple, cast

from anthropic import AsyncAnthropic
from anthropic.types import MessageParam
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel

from arena.core.runlog import get_runlog
from arena.models.pricing import cost_usd

DEFAULT_MAX_TOKENS = 8192
OLLAMA_BASE_URL = "http://localhost:11434/v1"
# Cold-loading a 14B+ model into VRAM can take a while before the first token.
OLLAMA_TIMEOUT_S = 600


class ModelResponse(BaseModel):
    text: str
    reasoning: str | None = None
    input_tokens: int
    output_tokens: int  # includes reasoning_tokens
    reasoning_tokens: int = 0
    latency_ms: int
    cost_usd: float | None  # None when the model has no known price
    model: str
    truncated: bool = False  # stopped at max_tokens, so the answer may be cut off


class _Raw(NamedTuple):
    text: str
    reasoning: str | None
    input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    truncated: bool


# Claude models that think adaptively when `thinking` is omitted. For these the
# router sends that same setting explicitly, only to ask for a readable summary
# (see anthropic_thinking). Other models get no `thinking` unless asked.
_ANTHROPIC_ADAPTIVE_BY_DEFAULT = {
    "claude-fable-5-1",
    "claude-opus-5-5",
    "claude-sonnet-5-5",
    "claude-haiku-5-5",
}

# Turning thinking off takes a different setting per model, and some models
# can't turn it off.
_ANTHROPIC_THINKING_OFF: dict[str, dict] = {
    "claude-haiku-5-5": {"type": "disabled"},
    "claude-sonnet-5-5": {"type": "between_tools"},
}


def can_disable_thinking(model: str) -> bool:
    """Whether `think: false` means anything for this provider/model string."""
    provider, _, name = model.partition("/")
    return provider != "anthropic" or name in _ANTHROPIC_THINKING_OFF


_anthropic: AsyncAnthropic | None = None
_ollama: AsyncOpenAI | None = None


def _anthropic_client() -> AsyncAnthropic:
    global _anthropic
    if _anthropic is None:
        _anthropic = AsyncAnthropic()  # reads ANTHROPIC_API_KEY
    return _anthropic


def _ollama_client() -> AsyncOpenAI:
    global _ollama
    if _ollama is None:
        _ollama = AsyncOpenAI(
            base_url=os.environ.get("OLLAMA_BASE_URL", OLLAMA_BASE_URL),
            api_key="ollama",  # required by the SDK, ignored by Ollama
            timeout=OLLAMA_TIMEOUT_S,
        )
    return _ollama


async def complete(
    model: str,
    system: str,
    messages: list[dict],
    *,
    think: bool | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    run_id: int | None = None,
    problem_id: str | None = None,
    role: str | None = None,
) -> ModelResponse:
    """Send one chat turn to `model` and return the answer with reasoning split out.

    think=None keeps the model's default thinking behaviour; True/False asks for
    thinking on/off. When an Anthropic model thinks, `reasoning` is the API's
    summary of it (the raw chain of thought is never returned) while
    reasoning_tokens counts all of it.
    run_id, problem_id and role only tag the run log entry.
    """
    provider, _, name = model.partition("/")
    log_call = partial(
        get_runlog().log_call,
        run_id=run_id,
        problem_id=problem_id,
        role=role,
        model=model,
        think=think,
        system=system,
        messages=messages,
    )

    start = time.perf_counter()
    try:
        if provider == "anthropic":
            raw = await _complete_anthropic(name, system, messages, think, max_tokens)
        elif provider == "ollama":
            raw = await _complete_ollama(name, system, messages, think, max_tokens)
        else:
            raise ValueError(f"unknown provider in model string: {model!r}")
    except (Exception, asyncio.CancelledError) as e:
        # CancelledError is a run stopped by Ctrl-C/SIGTERM; log it so the
        # transcript shows the call that was in flight.
        latency_ms = int((time.perf_counter() - start) * 1000)
        error = "interrupted" if isinstance(e, asyncio.CancelledError) else f"{type(e).__name__}: {e}"
        log_call(latency_ms=latency_ms, error=error)
        raise
    latency_ms = int((time.perf_counter() - start) * 1000)

    response = ModelResponse(
        **raw._asdict(),
        latency_ms=latency_ms,
        cost_usd=cost_usd(model, raw.input_tokens, raw.output_tokens),
        model=model,
    )
    log_call(
        input_tokens=response.input_tokens,
        output_tokens=response.output_tokens,
        reasoning_tokens=response.reasoning_tokens,
        latency_ms=latency_ms,
        cost_usd=response.cost_usd,
        response=response.text,
        reasoning=response.reasoning,
        truncated=response.truncated,
    )
    return response


async def _complete_anthropic(
    name: str, system: str, messages: list[dict], think: bool | None, max_tokens: int
) -> _Raw:
    kwargs: dict = {}
    if system:
        kwargs["system"] = system
    thinking = anthropic_thinking(name, think)
    if thinking is not None:
        kwargs["thinking"] = thinking

    msg = await _anthropic_client().messages.create(
        model=name, messages=cast(list[MessageParam], messages), max_tokens=max_tokens, **kwargs
    )

    text = "".join(block.text for block in msg.content if block.type == "text")
    reasoning = "\n\n".join(block.thinking for block in msg.content if block.type == "thinking")
    details = msg.usage.output_tokens_details
    return _Raw(
        text=text,
        reasoning=reasoning or None,
        input_tokens=msg.usage.input_tokens,
        output_tokens=msg.usage.output_tokens,
        reasoning_tokens=(details.thinking_tokens or 0) if details else 0,
        truncated=msg.stop_reason == "max_tokens",
    )


def anthropic_thinking(name: str, think: bool | None) -> dict | None:
    """The `thinking` request parameter for a Claude model, or None to omit it."""
    if think is False:
        if name not in _ANTHROPIC_THINKING_OFF:
            raise ValueError(f"{name} cannot turn thinking off")
        # between_tools accepts no other field, so its thinking can't be summarized.
        return _ANTHROPIC_THINKING_OFF[name]
    if think is None and name not in _ANTHROPIC_ADAPTIVE_BY_DEFAULT:
        return None
    # The API's default display is "omitted", which returns empty thinking text
    # and leaves the run log nothing to read. A summary bills the same.
    return {"type": "adaptive", "display": "summarized"}


async def _complete_ollama(
    name: str, system: str, messages: list[dict], think: bool | None, max_tokens: int
) -> _Raw:
    kwargs: dict = {}
    # Thinking models think by default, so only "off" needs a flag. gpt-oss
    # cannot fully disable thinking and still produces a short trace.
    if think is False:
        kwargs["reasoning_effort"] = "none"

    chat = [{"role": "system", "content": system}, *messages] if system else messages
    resp = await _ollama_client().chat.completions.create(
        model=name, messages=cast(list[ChatCompletionMessageParam], chat), max_tokens=max_tokens, **kwargs
    )

    choice = resp.choices[0]
    message = choice.message
    # Ollama returns thinking in a non-standard `reasoning` field; some models
    # put it inline in <think> tags instead.
    field_reasoning = (message.model_extra or {}).get("reasoning")
    text, tag_reasoning = split_think_tags(message.content or "")
    reasoning = "\n\n".join(r for r in (field_reasoning, tag_reasoning) if r) or None

    usage = resp.usage
    input_tokens = usage.prompt_tokens if usage else 0
    output_tokens = usage.completion_tokens if usage else 0
    details = usage.completion_tokens_details if usage else None
    if details and details.reasoning_tokens is not None:
        reasoning_tokens = details.reasoning_tokens
    else:
        reasoning_tokens = estimate_reasoning_tokens(output_tokens, reasoning, text)

    truncated = choice.finish_reason == "length"
    return _Raw(text, reasoning, input_tokens, output_tokens, reasoning_tokens, truncated)


_THINK_BLOCK = re.compile(r"<think>(.*?)</think>", re.DOTALL)


def split_think_tags(content: str) -> tuple[str, str | None]:
    """Split inline <think> reasoning out of a response. Returns (text, reasoning)."""
    parts: list[str] = []

    # Some chat templates open the think block inside the prompt, so the
    # response starts mid-thought and only the closing tag appears.
    close = content.find("</think>")
    open_ = content.find("<think>")
    if close != -1 and (open_ == -1 or close < open_):
        parts.append(content[:close])
        content = content[close + len("</think>") :]

    parts.extend(_THINK_BLOCK.findall(content))
    content = _THINK_BLOCK.sub("", content)

    # An unclosed <think> means the output was cut off mid-thought.
    if "<think>" in content:
        content, _, tail = content.partition("<think>")
        parts.append(tail)

    reasoning = "\n\n".join(p.strip() for p in parts if p.strip())
    return content.strip(), reasoning or None


def estimate_reasoning_tokens(output_tokens: int, reasoning: str | None, text: str) -> int:
    """Ollama reports one completion count covering thinking and answer.

    Split it by character share. This is an estimate, not a tokenizer count.
    """
    if not reasoning:
        return 0
    chars = len(reasoning) + len(text)
    return round(output_tokens * len(reasoning) / chars)
