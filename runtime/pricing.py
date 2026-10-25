"""Per-model token prices and the cost of one run.

Prices are configuration, not code: edit `PRICES` when Anthropic changes them, and update the
"checked" date on the entry. A model without an entry costs `None`, never zero, so a missing price
shows up in the trace as a null `cost_usd` instead of a free run.

Model ids here are not verified against the API. The live test that calls `client.models.list()`
and compares the result with `PRICES` lives with the Anthropic client wrapper (#7).
"""

import logging
from dataclasses import dataclass
from types import MappingProxyType

logger = logging.getLogger(__name__)

_PER_MILLION = 1_000_000


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """USD per million tokens for one model.

    `cache_write` is the 5-minute cache write price. The trace keeps one `cache_write_tokens`
    count and does not split it by TTL, so runs that use the 1-hour cache are costed slightly low.
    """

    input: float
    output: float
    cache_read: float
    cache_write: float


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Tokens one run used, summed over all turns. `None` counts as zero.

    As in the API's usage, `input_tokens` excludes cache reads and writes.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None


# Checked 2026-10-25 against the Claude API model and pricing reference (cached 2026-10-06).
# Haiku 5.5 charges a higher rate card for prompts over 100K tokens ($0.50 in, $2.50 out). A flat
# per-model price cannot express that, so long Haiku prompts are costed low.
_PRICES: dict[str, ModelPrice] = {
    # checked 2026-10-25
    "claude-fable-5-1": ModelPrice(input=10.0, output=50.0, cache_read=0.25, cache_write=12.5),
    # checked 2026-10-25
    "claude-opus-5-5": ModelPrice(input=4.0, output=20.0, cache_read=0.20, cache_write=5.0),
    # checked 2026-10-25
    "claude-sonnet-5-5": ModelPrice(input=2.0, output=10.0, cache_read=0.20, cache_write=2.5),
    # checked 2026-10-25, prompts of 100K tokens or fewer
    "claude-haiku-5-5": ModelPrice(input=0.10, output=0.50, cache_read=0.01, cache_write=0.125),
}

PRICES = MappingProxyType(_PRICES)

# Models already warned about in this process.
_warned: set[str] = set()


def cost_usd(model: str, usage: TokenUsage) -> float | None:
    """Return the USD cost of `usage` on `model`, or `None` when the model has no price.

    The first lookup of an unknown model in a process logs one warning; later lookups are silent.
    """
    price = PRICES.get(model)
    if price is None:
        if model not in _warned:
            _warned.add(model)
            logger.warning("no price for model %r; cost_usd will be null", model)
        return None
    return (
        (usage.input_tokens or 0) * price.input
        + (usage.output_tokens or 0) * price.output
        + (usage.cache_read_tokens or 0) * price.cache_read
        + (usage.cache_write_tokens or 0) * price.cache_write
    ) / _PER_MILLION
