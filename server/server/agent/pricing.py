"""Gemini prices on Vertex AI, used to estimate what a run cost.

Source: https://cloud.google.com/model-garden/pricing (the Vertex AI pricing page redirects
there), read 2026-11-26. Standard pay-as-you-go, global endpoint, text, USD per 1M tokens.
"Text output (response and reasoning)": thinking tokens are billed as output. Cached
input tokens (Vertex AI caches repeated prompt prefixes implicitly) cost the "cache read"
price. Only requests that return 200 are charged. Gemini 3.8 Flash and 3.7 Flash have an
introductory $0.75 / $3.75 through 2026-12-31 and $1.50 / $7.50 from 2027-01-01.

These are estimates for the run details; the Cloud Billing report is the real number.
Models not listed here get no estimate (the UI then asks for a rate in Settings)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

SOURCE = "https://cloud.google.com/model-garden/pricing"
CHECKED = date(2026, 11, 26)


@dataclass(frozen=True)
class Price:
    input_usd_per_m: float
    output_usd_per_m: float
    cached_usd_per_m: float
    until: date | None = None  # last day this price applies; None = open-ended


_FLASH = [Price(0.75, 3.75, 0.075, until=date(2026, 12, 31)), Price(1.50, 7.50, 0.15)]

PRICES: dict[str, list[Price]] = {
    "gemini-3.8-flash": _FLASH,
    "gemini-3.7-flash": _FLASH,
    "gemini-4-hydrogen": [Price(1.50, 7.50, 0.075)],
}


def price(model: str, on: date) -> Price | None:
    for p in PRICES.get(model, []):
        if p.until is None or on <= p.until:
            return p
    return None


def cost_usd(
    model: str, tokens_in: int, tokens_out: int, on: date, cached: int = 0
) -> float | None:
    """tokens_in includes `cached`; tokens_out includes thinking (billed as output)."""
    p = price(model, on)
    if p is None:
        return None
    fresh = max(tokens_in - cached, 0)
    return (
        fresh / 1e6 * p.input_usd_per_m
        + cached / 1e6 * p.cached_usd_per_m
        + tokens_out / 1e6 * p.output_usd_per_m
    )
