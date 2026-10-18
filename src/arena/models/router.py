"""One async interface over Anthropic and Ollama.

Model strings are prefixed by provider: "anthropic/claude-sonnet-5-5",
"ollama/qwen2.5-coder:14b". Every call is written to the run log, including
failed ones.
"""

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


# Current Claude models think adaptively when `thinking` is omitted. Turning it
# off takes a different setting per model, and some models can't turn it off.
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

    think=None leaves the model's default; True/False asks for thinking on/off.
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
    except Exception as e:
        latency_ms = int((time.perf_counter() - start) * 1000)
        log_call(latency_ms=latency_ms, error=f"{type(e).__name__}: {e}")
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
    if think is True:
        kwargs["thinking"] = {"type": "adaptive"}
    elif think is False:
        if name not in _ANTHROPIC_THINKING_OFF:
            raise ValueError(f"{name} cannot turn thinking off")
        kwargs["thinking"] = _ANTHROPIC_THINKING_OFF[name]

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
