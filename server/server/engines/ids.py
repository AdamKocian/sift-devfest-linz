"""Stable, readable signal ids derived from what identifies the data in Grail."""

from __future__ import annotations

import hashlib
import re

_WORD = re.compile(r"[a-z0-9]+")


def slug(text: str, max_words: int = 6) -> str:
    words = _WORD.findall(text.lower())
    return "-".join(words[:max_words]) or "x"


def log_source(service: str, source: str, level: str) -> str:
    return f"log-{slug(service)}-{slug(source)}-{slug(level)}"


def log_pattern(parent: str, template: str) -> str:
    literal = " ".join(t for t in template.split() if not t.startswith("<"))
    digest = hashlib.sha1(template.encode()).hexdigest()[:4]
    return f"{parent}-{slug(literal, 3)}-{digest}"


def span(service: str, name: str) -> str:
    return f"span-{slug(service)}-{slug(name)}"


def span_attribute(span_id: str, key: str) -> str:
    return f"{span_id}-attr-{slug(key)}"


def metric(service: str, key: str) -> str:
    return f"metric-{slug(service)}-{slug(key, 8)}"


def metric_dimension(metric_id: str, key: str) -> str:
    return f"{metric_id}-dim-{slug(key)}"
