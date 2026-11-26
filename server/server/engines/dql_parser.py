"""Deterministic DQL reader for usage: which data a query reads, which fields it uses.

It is not a full DQL parser. It understands the parts that decide attribution: the data
source (fetch / timeseries / metrics), filters (==, !=, in(), matchesValue, matchesPhrase,
contains, and/or/not), referenced fields and metric keys, and dashboard variables ($name).
Anything it cannot read with certainty (lookup/join/append, unknown commands, variables
without a static value, filters too complex to expand) comes back `unresolved` with a reason,
so the caller can hand it to Gemini instead of guessing.

Syntax reference: https://docs.dynatrace.com/docs/platform/grail/dynatrace-query-language
Dashboard variables ($key, :noquote, :backtick, :triplequote):
https://docs.dynatrace.com/docs/analyze-explore-automate/dashboards-and-notebooks/document-api/document-structure-dashboards
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import product
from typing import Any

MAX_TERMS = 32

# --- tokens -----------------------------------------------------------------------------

_TOKEN = re.compile(
    r"""
    (?P<ws>\s+|//[^\n]*)
  | (?P<tstr>\"\"\"(?:.|\n)*?\"\"\")
  | (?P<str>"(?:\\.|[^"\\])*")
  | (?P<btick>`[^`]*`)
  | (?P<var>\$[A-Za-z_][A-Za-z0-9_]*(?::(?:noquote|backtick|triplequote))?)
  | (?P<num>\d+(?:\.\d+)?[A-Za-z]*)
  | (?P<ident>[A-Za-z_@][A-Za-z0-9_.@]*(?<!\.))
  | (?P<op>==|!=|>=|<=|=|>|<|\+|-|\*|/|%)
  | (?P<punct>[(){}\[\],:|])
    """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class Tok:
    kind: str
    text: str


class DqlSyntaxError(ValueError):
    pass


def tokenize(dql: str) -> list[Tok]:
    out: list[Tok] = []
    pos = 0
    while pos < len(dql):
        m = _TOKEN.match(dql, pos)
        if not m:
            raise DqlSyntaxError(f"unexpected character {dql[pos]!r} at {pos}")
        pos = m.end()
        kind = m.lastgroup or ""
        if kind == "ws":
            continue
        text = m.group()
        if kind == "tstr":
            out.append(Tok("str", text[3:-3]))
        elif kind == "str":
            out.append(Tok("str", re.sub(r"\\(.)", r"\1", text[1:-1])))
        elif kind == "btick":
            out.append(Tok("ident", text[1:-1]))
        else:
            out.append(Tok(kind, text))
    return out


# --- variables --------------------------------------------------------------------------


@dataclass(frozen=True)
class Variable:
    """A dashboard variable with a static value (text, csv, or a resolved default)."""

    key: str
    values: tuple[str, ...]
    multiple: bool = False


def substitute(dql: str, variables: Mapping[str, Variable]) -> tuple[str, list[str]]:
    """Replace $vars by their static values. Returns (dql, missing variable names)."""
    missing: list[str] = []

    def repl(m: re.Match[str]) -> str:
        name, _, mod = m.group(1), m.group(2), m.group(3) or ""
        var = variables.get(name)
        if var is None or not var.values:
            missing.append(name)
            return m.group(0)
        if mod in ("noquote", "backtick"):
            wrap = "`" if mod == "backtick" else ""
            return ", ".join(f"{wrap}{v}{wrap}" for v in var.values)
        lits = ['"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"' for v in var.values]
        if mod == "triplequote":
            lits = [f'"""{v}"""' for v in var.values]
        return f"array({', '.join(lits)})" if var.multiple else lits[0]

    out = re.sub(r"\$([A-Za-z_][A-Za-z0-9_]*)(:(noquote|backtick|triplequote))?", repl, dql)
    return out, missing


# --- expressions ------------------------------------------------------------------------


@dataclass(frozen=True)
class Node:
    kind: str  # or | and | not | cmp | call | field | lit | array | arith | other
    op: str = ""
    args: tuple[Any, ...] = ()
    named: tuple[tuple[str, Any], ...] = ()
    value: Any = None


KEYWORDS = {"and", "or", "not", "xor", "true", "false", "null", "asc", "desc"}


class _Expr:
    """Pratt parser over a token slice."""

    def __init__(self, toks: Sequence[Tok]) -> None:
        self.toks = list(toks)
        self.i = 0

    def peek(self, k: int = 0) -> Tok | None:
        j = self.i + k
        return self.toks[j] if j < len(self.toks) else None

    def take(self) -> Tok:
        t = self.peek()
        if t is None:
            raise DqlSyntaxError("unexpected end of expression")
        self.i += 1
        return t

    def expect(self, text: str) -> None:
        t = self.take()
        if t.text != text:
            raise DqlSyntaxError(f"expected {text!r}, got {t.text!r}")

    def at(self, text: str) -> bool:
        t = self.peek()
        return t is not None and t.text == text and t.kind in ("punct", "op", "ident")

    def parse(self) -> Node:
        node = self.or_()
        if self.peek() is not None:
            raise DqlSyntaxError(f"unexpected {self.peek().text!r}")  # type: ignore[union-attr]
        return node

    def or_(self) -> Node:
        node = self.and_()
        while self.at("or") or self.at("xor"):
            op = self.take().text
            right = self.and_()
            node = (
                Node("or", args=(node, right)) if op == "or" else Node("other", args=(node, right))
            )
        return node

    def and_(self) -> Node:
        node = self.not_()
        while self.at("and"):
            self.take()
            node = Node("and", args=(node, self.not_()))
        return node

    def not_(self) -> Node:
        if self.at("not"):
            self.take()
            return Node("not", args=(self.not_(),))
        return self.cmp()

    def cmp(self) -> Node:
        left = self.arith()
        t = self.peek()
        if t is not None and t.kind == "op" and t.text in ("==", "!=", ">=", "<=", ">", "<"):
            self.take()
            return Node("cmp", op=t.text, args=(left, self.arith()))
        return left

    def arith(self) -> Node:
        node = self.unary()
        while (t := self.peek()) is not None and t.kind == "op" and t.text in "+-*/%":
            self.take()
            node = Node("arith", op=t.text, args=(node, self.unary()))
        return node

    def unary(self) -> Node:
        if self.at("-"):
            self.take()
            return Node("arith", op="neg", args=(self.unary(),))
        return self.primary()

    def primary(self) -> Node:
        t = self.take()
        if t.kind == "str":
            return Node("lit", value=t.text)
        if t.kind == "num":
            return Node("lit", value=t.text)
        if t.kind == "var":
            raise DqlSyntaxError(f"unresolved variable {t.text}")
        if t.text == "(":
            node = self.or_()
            self.expect(")")
            return node
        if t.text == "{":
            items = self.items("}")
            return Node("array", args=tuple(v for _, v in items))
        if t.kind == "ident":
            if t.text in ("true", "false", "null"):
                return Node("lit", value=t.text)
            if self.at("("):
                self.take()
                items = self.items(")")
                return Node(
                    "call",
                    op=t.text,
                    args=tuple(v for k, v in items if k is None),
                    named=tuple((k, v) for k, v in items if k is not None),
                )
            return Node("field", value=t.text)
        raise DqlSyntaxError(f"unexpected {t.text!r}")

    def items(self, close: str) -> list[tuple[str | None, Node]]:
        out: list[tuple[str | None, Node]] = []
        while not self.at(close):
            name = None
            a, b = self.peek(), self.peek(1)
            if a is not None and b is not None and a.kind == "ident" and b.text == ":":
                name = a.text
                self.i += 2
            if (
                a is not None
                and b is not None
                and a.kind == "ident"
                and b.text == "="
                and close == "}"
            ):
                self.i += 2  # `by:{alias = expr}`; the alias is a new name, not a field
            out.append((name, self.or_()))
            if self.at(","):
                self.take()
            elif not self.at(close):
                raise DqlSyntaxError(f"expected ',' or {close!r}")
        self.take()
        return out


def parse_expr(toks: Sequence[Tok]) -> Node:
    return _Expr(toks).parse()


def fields_of(node: Node, derived: set[str] | None = None) -> set[str]:
    out: set[str] = set()
    derived = derived or set()

    def walk(n: Any) -> None:
        if not isinstance(n, Node):
            return
        if n.kind == "field" and n.value not in derived and n.value not in KEYWORDS:
            out.add(n.value)
        for a in n.args:
            walk(a)
        for _, v in n.named:
            walk(v)

    walk(node)
    return out


# --- filters as DNF ---------------------------------------------------------------------


@dataclass
class Term:
    """One conjunction: field must be one of `eq[field]`, none of `ne[field]`, and the
    record must contain every phrase."""

    eq: dict[str, set[str]] = field(default_factory=dict)
    ne: dict[str, set[str]] = field(default_factory=dict)
    phrases: list[tuple[str, str]] = field(default_factory=list)

    def merge(self, other: Term) -> Term | None:
        eq = {k: set(v) for k, v in self.eq.items()}
        for k, v in other.eq.items():
            eq[k] = eq[k] & v if k in eq else set(v)
            if not eq[k]:
                return None  # contradiction: field == a and field == b
        ne = {k: set(v) for k, v in self.ne.items()}
        for k, v in other.ne.items():
            ne.setdefault(k, set()).update(v)
        return Term(eq, ne, [*self.phrases, *other.phrases])

    def allows(self, f: str, value: str | None, case_insensitive: bool = False) -> bool:
        def norm(v: str) -> str:
            return v.lower() if case_insensitive else v

        if value is None:
            return f not in self.eq
        if f in self.eq and norm(value) not in {norm(v) for v in self.eq[f]}:
            return False
        return not (f in self.ne and norm(value) in {norm(v) for v in self.ne[f]})


class TooComplexError(ValueError):
    pass


PHRASE_FUNCS = {"matchesPhrase", "contains", "startsWith", "endsWith", "matchesValue"}


def _lit_values(n: Node) -> list[str] | None:
    """Literal values of a literal or a (nested) array of literals."""
    if n.kind == "lit":
        return [str(n.value)]
    if n.kind == "array" or (n.kind == "call" and n.op == "array"):
        vals: list[str] = []
        for a in n.args:
            inner = _lit_values(a) if isinstance(a, Node) else None
            if inner is None:
                return None
            vals.extend(inner)
        return vals
    return None


def _atom(n: Node, negate: bool) -> list[Term]:
    """A leaf condition as DNF. Unknown conditions do not restrict (conservative)."""
    if n.kind == "cmp" and n.op in ("==", "!="):
        left, right = n.args
        if right.kind == "field" and left.kind == "lit":
            left, right = right, left
        vals_list = _lit_values(right) if left.kind == "field" else None
        if vals_list is not None:  # `field == "a"`, or `field == array(...)` from a variable
            positive = (n.op == "==") != negate
            vals = set(vals_list)
            return [Term(eq={left.value: vals})] if positive else [Term(ne={left.value: vals})]
    if n.kind == "call" and n.op == "in" and len(n.args) >= 2 and n.args[0].kind == "field":
        vals: list[str] = []
        for a in n.args[1:]:
            v = _lit_values(a)
            if v is None:
                return [Term()]
            vals.extend(v)
        f = n.args[0].value
        return [Term(ne={f: set(vals)})] if negate else [Term(eq={f: set(vals)})]
    if n.kind == "call" and n.op in PHRASE_FUNCS and len(n.args) >= 2:
        target, arg = n.args[0], n.args[1]
        if target.kind == "field" and arg.kind == "lit":
            text = str(arg.value)
            if n.op == "matchesValue" and "*" not in text:
                vals = {text}
                return (
                    [Term(ne={target.value: vals})] if negate else [Term(eq={target.value: vals})]
                )
            if not negate:
                return [Term(phrases=[(target.value, text.replace("*", " "))])]
    return [Term()]


def dnf(n: Node, negate: bool = False) -> list[Term]:
    if n.kind == "not":
        return dnf(n.args[0], not negate)
    if n.kind in ("and", "or"):
        conj = (n.kind == "and") != negate  # De Morgan
        left, right = dnf(n.args[0], negate), dnf(n.args[1], negate)
        if not conj:
            out = left + right
        else:
            out = [t for a, b in product(left, right) if (t := a.merge(b)) is not None]
        if len(out) > MAX_TERMS:
            raise TooComplexError(f"filter expands to more than {MAX_TERMS} cases")
        return out
    return _atom(n, negate)


def and_dnf(a: list[Term], b: list[Term]) -> list[Term]:
    out = [t for x, y in product(a, b) if (t := x.merge(y)) is not None]
    if len(out) > MAX_TERMS:
        raise TooComplexError(f"filter expands to more than {MAX_TERMS} cases")
    return out


# --- commands ---------------------------------------------------------------------------


@dataclass
class ParsedQuery:
    source: str  # logs | spans | metrics | events | bizevents | dt.system.events | ...
    terms: list[Term] = field(default_factory=lambda: [Term()])
    fields: set[str] = field(default_factory=set)
    metric_keys: list[str] = field(default_factory=list)
    projected: bool = False  # output reduced to named fields / aggregates
    unresolved: str | None = None


READS_OTHER_DATA = {"lookup", "join", "joinNested", "append", "load"}
PROJECTING = {"fields", "fieldsKeep", "summarize", "makeTimeseries", "fieldsSummary"}
PASS_THROUGH = {
    "sort", "limit", "dedup", "fieldsAdd", "fieldsRemove", "fieldsRename", "parse", "expand",
    "filter", "filterOut", "fieldsFlatten", "timeseries",
}  # fmt: skip


def _split(toks: list[Tok], sep: str) -> list[list[Tok]]:
    parts: list[list[Tok]] = [[]]
    depth = 0
    for t in toks:
        if t.kind == "punct" and t.text in "({[":
            depth += 1
        elif t.kind == "punct" and t.text in ")}]":
            depth -= 1
        if depth == 0 and t.kind == "punct" and t.text == sep:
            parts.append([])
        else:
            parts[-1].append(t)
    return [p for p in parts if p]


def _args(toks: list[Tok]) -> list[tuple[str | None, str | None, list[Tok]]]:
    """Command arguments: (param name, alias, expression tokens)."""
    out = []
    for part in _split(toks, ","):
        name = alias = None
        if len(part) > 2 and part[0].kind == "ident" and part[1].text == ":":
            name, part = part[0].text, part[2:]
        elif len(part) > 2 and part[0].kind == "ident" and part[1].text == "=":
            alias, part = part[0].text, part[2:]
        out.append((name, alias, part))
    return out


def _expr_fields(toks: list[Tok], derived: set[str]) -> set[str]:
    return fields_of(parse_expr(toks), derived)


def _metric_keys(node: Node) -> list[str]:
    """Metric keys inside timeseries aggregations: avg(key), percentile(key, 95), ..."""
    keys: list[str] = []

    def walk(n: Any) -> None:
        if not isinstance(n, Node):
            return
        if (
            n.kind == "call"
            and n.args
            and isinstance(n.args[0], Node)
            and n.args[0].kind == "field"
        ):
            keys.append(n.args[0].value)
            return
        for a in n.args:
            walk(a)

    walk(node)
    return keys


def parse(dql: str, variables: Mapping[str, Variable] | None = None) -> ParsedQuery:
    text, missing = substitute(dql, variables or {})
    if missing:
        names = ", ".join(f"${m}" for m in sorted(set(missing)))
        return ParsedQuery("unknown", unresolved=f"variable {names} has no static value")
    try:
        return _parse(tokenize(text))
    except TooComplexError as exc:
        return ParsedQuery("unknown", unresolved=str(exc))
    except (DqlSyntaxError, IndexError, ValueError) as exc:
        return ParsedQuery("unknown", unresolved=f"could not parse: {exc}")


def _parse(toks: list[Tok]) -> ParsedQuery:
    commands = _split(toks, "|")
    if not commands:
        raise DqlSyntaxError("empty query")
    head, rest = commands[0], commands[1:]
    verb = head[0].text
    derived: set[str] = set()
    if verb == "fetch":
        args = _args(head[1:])
        if not args or args[0][0] is not None:
            raise DqlSyntaxError("fetch without data object")
        source = " ".join(t.text for t in args[0][2])
        q = ParsedQuery(source)
    elif verb == "timeseries":
        q = ParsedQuery("metrics", projected=True)
        for name, alias, part in _args(head[1:]):
            if name == "by":
                q.fields |= _expr_fields(part, derived)
            elif name == "filter":
                node = parse_expr(part)
                q.fields |= fields_of(node)
                q.terms = and_dnf(q.terms, dnf(node))
            elif name is None:
                node = parse_expr(part)
                q.metric_keys.extend(_metric_keys(node))
                if alias:
                    derived.add(alias)
    elif verb == "metrics":
        q = ParsedQuery("metrics.series")
    else:
        return ParsedQuery("unknown", unresolved=f"starts with `{verb}`, which Sift does not read")

    for cmd in rest:
        verb, body = cmd[0].text, cmd[1:]
        if verb in READS_OTHER_DATA:
            q.unresolved = f"uses `{verb}`, which reads other data"
            return q
        if verb not in PASS_THROUGH and verb not in PROJECTING:
            q.unresolved = f"unknown command `{verb}`"
            return q
        if verb in ("filter", "filterOut"):
            node = parse_expr(body)
            q.fields |= fields_of(node, derived)
            q.terms = and_dnf(q.terms, dnf(node, negate=verb == "filterOut"))
            continue
        for name, alias, part in _args(body):
            if verb == "parse" and name is None and part and part[0].kind == "str":
                continue  # the parse pattern string
            if verb == "limit" or (verb == "sort" and name is None and not part):
                continue
            if part and part[-1].kind == "ident" and part[-1].text in ("asc", "desc"):
                part = part[:-1]
            if part:
                q.fields |= _expr_fields(part, derived)
            if alias:
                derived.add(alias)
            if verb in ("fields", "fieldsKeep") and alias is None and len(part) == 1:
                pass  # plain projection of an existing field
        if verb in PROJECTING:
            q.projected = True
    q.fields -= derived
    return q


def phrases_for(term: Term, content_fields: Iterable[str] = ("content",)) -> list[str]:
    allowed = set(content_fields)
    return [text for f, text in term.phrases if f in allowed]
