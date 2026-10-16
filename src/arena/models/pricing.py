"""Per-model token prices in USD per million tokens.

Local models cost nothing. Anthropic prices are from the Anthropic pricing page
as of 2026-10-16. Prompt caching prices are not tracked because the router does
not use prompt caching.
"""

from pydantic import BaseModel

# Above this many input tokens, models with a long_prompt price bill the whole
# call at that rate.
LONG_PROMPT_TOKENS = 100_000


class Price(BaseModel):
    input_per_mtok: float
    output_per_mtok: float
    long_prompt: "Price | None" = None


PRICES: dict[str, Price | None] = {
    "anthropic/claude-fable-5-1": Price(input_per_mtok=10.0, output_per_mtok=50.0),
    "anthropic/claude-opus-5-5": Price(input_per_mtok=4.0, output_per_mtok=20.0),
    "anthropic/claude-sonnet-5-5": Price(input_per_mtok=2.0, output_per_mtok=10.0),
    "anthropic/claude-haiku-5-5": Price(
        input_per_mtok=0.10,
        output_per_mtok=0.50,
        long_prompt=Price(input_per_mtok=0.50, output_per_mtok=2.50),
    ),
}

LOCAL = Price(input_per_mtok=0.0, output_per_mtok=0.0)


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Return the cost of a call, or None if the model has no known price.

    output_tokens already includes reasoning tokens, which bill as output.
    """
    price = LOCAL if model.startswith("ollama/") else PRICES.get(model)
    if price is None:
        return None
    if price.long_prompt and input_tokens > LONG_PROMPT_TOKENS:
        price = price.long_prompt
    return (input_tokens * price.input_per_mtok + output_tokens * price.output_per_mtok) / 1_000_000
