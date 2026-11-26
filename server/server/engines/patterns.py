"""Patterns: cluster sampled log lines into templates (Drain3) and redact examples.

Templates are mined on the first line of each record (a stack trace is one record with
many lines). A template becomes its own signal only when it dominates its log source: the
whole source is already a signal, and a pattern is only worth a rule when it is most of it.
The volume of each pattern is then measured exactly in Grail with a matchesPhrase() filter
built from the template's literal words, the same matcher a rule would use."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from drain3 import TemplateMiner
from drain3.masking import MaskingInstruction
from drain3.template_miner_config import TemplateMinerConfig

# Masks run before clustering: values that vary per line become typed placeholders.
MASKS: list[tuple[str, str]] = [
    (r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", "EMAIL"),
    (r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b", "UUID"),
    (r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "IP"),
    (r"\b[0-9a-fA-F]{16,}\b", "HEX"),
    (r"(?<![\w.])[-+]?\d+(?:\.\d+)?(?:ms|s|KB|MB|B)?\b", "NUM"),
]

# Redaction for examples shown in the UI and sent to Gemini: personal data and identifiers.
SENSITIVE_KEYS = (
    "txn|order|order_id|amount|total|actor|user|user_id|customer|token|card|iban|email|"
    "session|password|secret|auth|ip"
)
_REDACT = [
    (re.compile(MASKS[0][0]), "<email>"),
    (re.compile(MASKS[1][0]), "<uuid>"),
    (re.compile(MASKS[2][0]), "<ip>"),
    (re.compile(MASKS[3][0]), "<hex>"),
    (re.compile(rf"\b({SENSITIVE_KEYS})=([^\s,;]+)", re.IGNORECASE), r"\1=<redacted>"),
    (re.compile(r"\b\d{6,}\b"), "<num>"),
]


def redact(line: str) -> str:
    for rx, repl in _REDACT:
        line = rx.sub(repl, line)
    return line


@dataclass
class Template:
    template: str
    count: int = 0
    chars: int = 0
    examples: list[str] = field(default_factory=list)  # redacted, max 3

    def share(self, total: int) -> float:
        return self.count / total if total else 0.0


def _miner(sim_th: float, depth: int) -> TemplateMiner:
    cfg = TemplateMinerConfig()
    cfg.drain_sim_th = sim_th
    cfg.drain_depth = depth
    cfg.mask_prefix = "<"
    cfg.mask_suffix = ">"
    cfg.parametrize_numeric_tokens = True
    cfg.masking_instructions = [MaskingInstruction(p, name) for p, name in MASKS]
    cfg.profiling_enabled = False
    return TemplateMiner(config=cfg)


def first_line(content: str) -> tuple[str, int]:
    """(first line, number of extra lines)."""
    lines = content.splitlines() or [""]
    return lines[0].strip(), len(lines) - 1


def mine(contents: list[str], sim_th: float = 0.4, depth: int = 4) -> list[Template]:
    """Templates sorted by line count, most frequent first."""
    miner = _miner(sim_th, depth)
    by_cluster: dict[int, Template] = {}
    assigned: list[tuple[int, str, int]] = []
    for content in contents:
        line, extra = first_line(content)
        if not line:
            continue
        res: dict[str, Any] = miner.add_log_message(line)
        assigned.append((int(res["cluster_id"]), content, extra))
    for cid, content, extra in assigned:
        cluster = miner.drain.id_to_cluster[cid]
        t = by_cluster.setdefault(cid, Template(cluster.get_template()))
        t.count += 1
        t.chars += len(content)
        if len(t.examples) < 3:
            line, _ = first_line(content)
            suffix = f"  (+{extra} lines)" if extra else ""
            example = redact(line) + suffix
            if example not in t.examples:
                t.examples.append(example)
    return sorted(by_cluster.values(), key=lambda t: (-t.count, t.template))


def dominant(templates: list[Template], min_share: float = 0.6) -> list[Template]:
    """Templates worth their own signal: most of the source, and not the whole source."""
    total = sum(t.count for t in templates)
    if len(templates) < 2:
        return []
    return [t for t in templates if t.share(total) >= min_share]


_PLACEHOLDER = re.compile(r"<[^>]*>")
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


def phrase(template: str) -> str:
    """The longest run of literal words in a template, for matchesPhrase().

    matchesPhrase is token based (https://docs.dynatrace.com/docs/platform/grail/dynatrace-query-language/functions/string-functions),
    so a run is only plain words separated by spaces; it ends at a placeholder, at any token
    with punctuation inside, and after a word with trailing ':' ',' or ';'."""
    best: list[str] = []
    run: list[str] = []
    for tok in template.split():
        word = tok.rstrip(":,;")
        if _PLACEHOLDER.search(tok) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", word):
            run = []
            continue
        run.append(word)
        if len(" ".join(run)) > len(" ".join(best)):
            best = list(run)
        if word != tok:
            run = []
    return " ".join(best)


def matches(phrase_text: str, line: str) -> bool:
    """Local approximation of matchesPhrase (case-insensitive, consecutive tokens)."""
    want = [w.lower() for w in _TOKEN.findall(phrase_text)]
    if not want:
        return False
    have = [w.lower() for w in _TOKEN.findall(line)]
    n = len(want)
    return any(have[i : i + n] == want for i in range(len(have) - n + 1))


def display(template: str) -> str:
    """Drain3 placeholders for the UI: '<NUM>' -> '<*>', '<EMAIL>' -> '<email>'."""
    return re.sub(r"<([A-Z*]+)>", lambda m: "<email>" if m.group(1) == "EMAIL" else "<*>", template)
