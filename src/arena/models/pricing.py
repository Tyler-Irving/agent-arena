"""Per-model token prices in USD per million tokens.

Local models cost nothing. Anthropic prices are deliberately left unset until
they are filled in from the official pricing page, so cost shows as unknown
rather than a guessed number.
"""

from pydantic import BaseModel


class Price(BaseModel):
    input_per_mtok: float
    output_per_mtok: float


# TODO: fill in Anthropic prices from https://www.anthropic.com/pricing. Do not guess.
PRICES: dict[str, Price | None] = {
    "anthropic/claude-haiku-5-5": None,
    "anthropic/claude-sonnet-5-5": None,
    "anthropic/claude-opus-5-5": None,
}

LOCAL = Price(input_per_mtok=0.0, output_per_mtok=0.0)


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Return the cost of a call, or None if the model has no known price.

    output_tokens already includes reasoning tokens, which bill as output.
    """
    price = LOCAL if model.startswith("ollama/") else PRICES.get(model)
    if price is None:
        return None
    return (input_tokens * price.input_per_mtok + output_tokens * price.output_per_mtok) / 1_000_000
