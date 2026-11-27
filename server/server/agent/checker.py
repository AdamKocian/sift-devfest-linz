"""No invented numbers: every number in the agent's texts must appear in a tool result of
the same run (or in the user's own message).

Numbers are compared by value, not by spelling: "€1,321", "1321" and "1,321.0" are the
same number; units and currency signs are ignored. Citation markers like "[2]" are not
numbers. Digits glued to letters or hyphens (k8s, gemini-3.7-flash, INC-2291, SKU-1000)
are identifiers, not quantities, and are skipped."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from typing import Any

_CITATION = re.compile(r"\s*\[\d{1,2}\]")
_NUMBER = re.compile(r"(?<![\w.\-/#])\d[\d,]*(?:\.\d+)?")
_ORDINAL = re.compile(r"(?:st|nd|rd|th)\b")


def _value(token: str) -> Decimal | None:
    try:
        return Decimal(token.replace(",", "")).normalize()
    except InvalidOperation:
        return None


def numbers(text: str) -> list[str]:
    """Number tokens in a text, as written (without unit or currency sign)."""
    text = _CITATION.sub("", text)
    out = []
    for m in _NUMBER.finditer(text):
        if _ORDINAL.match(text, m.end()):
            continue
        out.append(m.group(0).rstrip(",."))
    return out


def _flatten(obj: Any) -> Iterable[str]:
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _flatten(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _flatten(v)
    elif isinstance(obj, bool) or obj is None:
        return
    elif isinstance(obj, (int, float)):
        yield repr(obj)
        if isinstance(obj, float):
            # a raw float may be shown rounded: 828.37 -> 828, 828.4
            yield f"{obj:.0f}"
            yield f"{obj:.1f}"
            yield f"{obj:.2f}"
    else:
        yield str(obj)


def known_values(sources: Iterable[Any]) -> set[Decimal]:
    out: set[Decimal] = set()
    for src in sources:
        text = src if isinstance(src, str) else " ".join(_flatten(src))
        for tok in _NUMBER.findall(_CITATION.sub("", text)):
            v = _value(tok.rstrip(",."))
            if v is not None:
                out.add(v)
    return out


def unbacked(texts: Iterable[str], sources: Iterable[Any]) -> list[str]:
    """Numbers in `texts` with no source. Empty list = the texts pass."""
    known = known_values(sources)
    missing: list[str] = []
    for t in texts:
        for tok in numbers(t):
            v = _value(tok)
            if v is not None and v not in known and tok not in missing:
                missing.append(tok)
    return missing


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
