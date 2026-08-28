"""Q20 cost telemetry: per-model pricing table.

Prices in USD per 1M tokens. Sourced from public pricing pages as of
2026-04. Pricing changes regularly — this table is the *default*; users
can override at deploy time via cloudless.yaml.

NOT a source of truth for billing — finance must reconcile via the cloud
provider's invoice. This is for *guard-rail* enforcement and rough cost
attribution rollups.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelPrice:
    """Price per 1M tokens, in USD."""
    input_per_million: float
    output_per_million: float
    cached_input_per_million: float = 0.0
    """If 0, treated as same as input (no cache discount)."""


# Defaults — match DEFAULT_ALIASES in cloudless.catalog.llm.
# Keep this conservative: when a model isn't here we fall back to a
# pessimistic estimate so cost caps don't silently underestimate.
DEFAULT_PRICES: dict[str, ModelPrice] = {
    # Amazon Nova (Bedrock, on-demand, us-east-1) — as of late 2025
    "us.amazon.nova-micro-v1:0": ModelPrice(0.035, 0.14),
    "us.amazon.nova-lite-v1:0": ModelPrice(0.06, 0.24),
    "us.amazon.nova-pro-v1:0": ModelPrice(0.80, 3.20),
    "us.amazon.nova-premier-v1:0": ModelPrice(2.50, 12.50),
    # Anthropic Claude on Bedrock
    "us.anthropic.claude-haiku-4-5-20251001-v1:0": ModelPrice(1.0, 5.0),
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0": ModelPrice(3.0, 15.0),
    "us.anthropic.claude-opus-4-7": ModelPrice(15.0, 75.0),
    # Gemini via Vertex
    "gemini-2.5-flash": ModelPrice(0.30, 2.50),
    "gemini-2.5-pro": ModelPrice(1.25, 10.0),
}


# Fallback for unknown model IDs — conservative (mid-tier pricing).
FALLBACK_PRICE = ModelPrice(input_per_million=3.0, output_per_million=15.0)


def estimate_cost_usd(
    model_id: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
    reasoning_tokens: int = 0,
    prices: dict[str, ModelPrice] | None = None,
) -> float:
    """Compute USD cost for a single LLM call from token counts.

    Reasoning tokens are billed at the output rate (Bedrock + Vertex both
    bill thinking tokens as output).
    """
    table = prices or DEFAULT_PRICES
    price = table.get(model_id, FALLBACK_PRICE)
    cached_rate = price.cached_input_per_million or price.input_per_million

    # Bedrock reports inputTokens INCLUDING cached. Cached has its own (cheaper) rate.
    fresh_input = max(0, input_tokens - cached_tokens)
    out_total = output_tokens + reasoning_tokens

    cost = (
        fresh_input * price.input_per_million / 1_000_000.0
        + cached_tokens * cached_rate / 1_000_000.0
        + out_total * price.output_per_million / 1_000_000.0
    )
    return cost
