"""Number formatting for sentences the server writes (explanations, run summaries).

The web client formats raw numbers with the same rules (web/src/format.ts)."""

from __future__ import annotations

from datetime import datetime


def gib(value: float) -> str:
    if value >= 100:
        return f"{value:,.0f} GiB"
    if value >= 10:
        return f"{value:,.1f} GiB"
    return f"{value:,.2f} GiB"


def eur(value: float) -> str:
    return f"€{value:,.0f}"


def count(value: float) -> str:
    if value >= 1e9:
        return f"{value / 1e9:.2f} B"
    if value >= 1e6:
        return f"{value / 1e6:.1f} M"
    if value >= 1e4:
        return f"{value / 1e3:.0f} k"
    return f"{value:,.0f}"


def pct(value: float) -> str:
    """0.976 -> '97.6 %' (one decimal below 99.95, else '100 %')."""
    v = value * 100
    return "100 %" if v >= 99.95 else f"{v:.1f} %"


def ago(then: datetime | None, now: datetime) -> str:
    if then is None:
        return "never"
    seconds = max(0, int((now - then).total_seconds()))
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} h ago"
    days = hours // 24
    return f"{days} days ago" if days != 1 else "1 day ago"
