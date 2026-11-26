"""SQLite tables (SQLModel). Derived numbers (cost, savings, usage class) are not stored:
they are computed from these rows by `server.engine` so they always agree with each other."""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Column
from sqlmodel import Field, SQLModel


def _json(default: Any = None) -> Any:
    factory = (lambda: None) if default is None else (lambda: type(default)(default))
    return Field(default_factory=factory, sa_column=Column(JSON))


class Signal(SQLModel, table=True):
    id: str = Field(primary_key=True)
    kind: str  # log_source | log_pattern | span | span_attribute | metric | metric_dimension
    data_type: str  # logs | spans | metrics
    service: str
    key: str
    parent_id: str | None = Field(default=None, index=True)
    source: str | None = None  # log source (app, nginx, audit)
    level: str | None = None  # log level for log_source signals
    gib_day: float  # baseline volume before any Sift rule
    avg_record_bytes: int
    growth_wow: float = 0.0  # week-over-week growth of the baseline
    first_seen: datetime
    finding_class: str | None = None
    finding_note: str | None = None
    samples: list[str] = _json([])


class Reference(SQLModel, table=True):
    """Usage: something that reads a signal (query execution, dashboard, alert, ...)."""

    id: int | None = Field(default=None, primary_key=True)
    signal_id: str = Field(index=True)
    type: str  # query | dashboard | notebook | workflow | alert | slo
    name: str
    owner: str
    count_30d: int = 0
    last_used: datetime | None = None
    fields: list[str] = _json([])
    dql: str | None = None
    note: str | None = None


class Proposal(SQLModel, table=True):
    id: str = Field(primary_key=True)
    signal_id: str = Field(index=True)
    title: str
    action: str  # drop | sample | drop_field | logs_to_metric | lower_level | redact | dedupe
    finding_class: str
    confidence: str  # high | medium | low
    status: str = Field(index=True)
    reduction_ratio: float  # share of the signal's volume the rule removes
    added_gib_day: float = 0.0  # e.g. the metric a logs_to_metric rule creates
    scope: dict[str, Any] = _json({})  # dry-run scope: which records the ratio is measured on
    explanation: list[str] = _json([])  # templates, filled by engine.render
    evidence: list[dict[str, Any]] = _json([])
    rule: dict[str, Any] = _json({})
    created_at: datetime
    run_id: str | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None
    reject_reason: str | None = None
    applied_at: datetime | None = None
    verify_due_at: datetime | None = None
    verified_at: datetime | None = None
    reverted_at: datetime | None = None
    revert_reason: str | None = None
    applied_target: str | None = None
    previous_state: dict[str, Any] | None = _json()
    verify_result: dict[str, Any] | None = _json()


class Run(SQLModel, table=True):
    id: str = Field(primary_key=True)
    type: str  # discover | analyze | verify
    status: str  # running | succeeded | failed
    trigger: str  # user | schedule
    started_at: datetime
    finished_at: datetime | None = None
    summary: str | None = None
    requested_model: str | None = None
    served_model: str | None = None
    fallback_reason: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    scripted: bool = True


class RunEvent(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    run_id: str = Field(index=True)
    seq: int
    at: datetime
    type: str  # step | tool_call | proposal | summary | end
    label: str
    status: str = "done"  # running | done | failed
    duration_ms: int | None = None
    tool: str | None = None
    rows: int | None = None
    result: dict[str, Any] | None = _json()


class AuditEvent(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    at: datetime = Field(index=True)
    actor: str  # "agent" or a person's name
    actor_type: str  # agent | user
    action: str
    subject_type: str  # proposal | run | settings | guardrail | demo
    subject_id: str | None = None
    summary: str
    before: dict[str, Any] | None = _json()
    after: dict[str, Any] | None = _json()


class Guardrail(SQLModel, table=True):
    id: str = Field(primary_key=True)
    builtin: bool = False
    name: str
    reason: str
    # builtin: a rule id (error_levels | audit_logs | referenced_90d); custom: field matcher
    rule: str | None = None
    service: str | None = None
    kind: str | None = None
    key_pattern: str | None = None  # fnmatch-style, e.g. "GET /static/*"
    created_by: str | None = None
    created_at: datetime


class Setting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: Any = Field(sa_column=Column(JSON))
