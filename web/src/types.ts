// Response shapes of the Sift API (server/server/views.py). Keep the two in sync.

export type DataType = 'logs' | 'spans' | 'metrics'
export type SignalKind =
  | 'log_source'
  | 'log_pattern'
  | 'span'
  | 'span_attribute'
  | 'metric'
  | 'metric_dimension'
export type UsageClass = 'used' | 'rarely' | 'unused'
export type ProposalStatus =
  | 'draft'
  | 'awaiting_approval'
  | 'approved'
  | 'applied'
  | 'verifying'
  | 'verified'
  | 'rejected'
  | 'reverted'
export type SignalStatus = ProposalStatus | 'protected' | UsageClass
export type StatusGroup = 'awaiting' | 'applied' | 'verified' | 'reverted' | 'rejected'
export type Action = 'drop' | 'sample' | 'drop_field' | 'logs_to_metric' | 'lower_level' | 'redact'
export type RefType = 'query' | 'dashboard' | 'notebook' | 'workflow' | 'alert' | 'slo'

export interface Usage {
  klass: UsageClass
  queries_30d: number
  by_type: Partial<Record<RefType, number>>
  last_used: string | null
  references_30d: number
}

export interface SignalSummary {
  id: string
  kind: SignalKind
  data_type: DataType
  service: string
  key: string
  parent_id: string | null
  level: string | null
  gib_day: number
  baseline_gib_day: number
  records_day: number
  cost_month: number
  usage: Usage
  status: SignalStatus
  protected_by: string[]
  proposal_id: string | null
  finding_class: string | null
}

export interface SeriesPoint {
  date: string
  gib: number
}

export interface Marker {
  date: string
  label: string
  proposal_id: string
}

export interface Reference {
  id: number
  type: RefType
  name: string
  owner: string
  count_30d: number
  last_used: string | null
  fields: string[]
  dql: string | null
  note: string | null
  signal_id: string
  signal_label: string
  effect: 'reads' | 'not_affected'
  effect_note: string | null
}

export interface SignalDetail extends SignalSummary {
  first_seen: string
  avg_record_bytes: number
  series: SeriesPoint[]
  markers: Marker[]
  samples: string[]
  finding: { klass: string; note: string | null } | null
  references: Reference[]
  proposals: ProposalSummary[]
  parent: SignalSummary | null
  children: SignalSummary[]
}

export interface Savings {
  gib_day: number
  gib_week: number
  gib_month: number
  eur_month: number
}

export interface ProposalSummary {
  id: string
  title: string
  action: Action
  kind: SignalKind
  data_type: DataType
  service: string
  signal_id: string
  signal_key: string
  status: ProposalStatus
  confidence: 'high' | 'medium' | 'low'
  finding_class: string
  savings: Savings
  usage: Usage
  created_at: string
  decided_by: string | null
  decided_at: string | null
  applied_at: string | null
  verify_due_at: string | null
  verified_at: string | null
  reverted_at: string | null
  failed_verification: boolean
}

export interface Evidence {
  n: number
  label: string
  target: 'volume' | 'impact' | 'rule' | 'guardrails'
  text: string
}

export interface RuleArtifact {
  code: string
  note: string
  stage?: string
  processor?: string
  matcher?: string
}

export interface VerifyCheck {
  name: string
  label: string
  passed: boolean
  detail: string
}

export interface ActivityItem {
  id: number
  at: string
  actor: string
  actor_type: 'agent' | 'user'
  action: string
  subject_type: 'proposal' | 'run' | 'settings' | 'guardrail' | 'demo'
  subject_id: string | null
  subject_title: string | null
  summary: string
  before: Record<string, unknown> | null
  after: Record<string, unknown> | null
}

export interface ProposalDetail extends ProposalSummary {
  explanation: string[]
  evidence: Evidence[]
  kpis: {
    gib_day: number
    records_day: number
    cost_month: number
    last_used: string | null
    references_30d: number
  }
  series: SeriesPoint[]
  markers: Marker[]
  samples: string[]
  rule: { openpipeline: RuleArtifact; otel: RuleArtifact }
  dry_run: {
    matched_records_day: number
    scope_records_day: number
    scope_label: string
    match_ratio: number
    errors_matched: number
  }
  impact: Reference[]
  blast_radius: Record<RefType, number>
  blocking_guardrails: string[]
  history: ActivityItem[]
  verify_result: {
    passed: boolean
    checks: VerifyCheck[]
    checked: string[]
    at: string
    incident?: string
  } | null
  revert_reason: string | null
  reject_reason: string | null
  run_id: string | null
  apply_target: string
  signal: SignalSummary
}

export interface RunSummary {
  id: string
  type: 'discover' | 'analyze' | 'verify'
  status: 'running' | 'succeeded' | 'failed'
  trigger: 'user' | 'schedule'
  started_at: string
  finished_at: string | null
  summary: string | null
  current_step: string | null
  steps_done: number
  steps_total: number
  requested_model: string | null
  served_model: string | null
  fallback_reason: string | null
  tokens_in: number
  tokens_out: number
  est_cost_usd: number | null
  scripted: boolean
}

export interface RunEvent {
  seq: number
  at: string
  type: 'step' | 'tool' | 'step_done' | 'proposal' | 'end'
  label: string
  status: string
  duration_ms: number | null
  tool: string | null
  rows: number | null
  result: Record<string, unknown> | null
}

export interface RunDetail extends RunSummary {
  events: RunEvent[]
}

export interface Overview {
  now: string
  kpis: {
    ingest_7d_gib: number
    ingest_prev_7d_gib: number
    cost_month_eur: number
    waste_gib_week: number
    waste_ratio: number
    waste_eur_month: number
    awaiting_count: number
    saved_eur_month: number
    verified_count: number
  }
  awaiting: ProposalSummary[]
  composition: { key: 'logs' | 'spans' | 'metrics' | 'events'; label: string; gib: number }[]
  failed_verifications: { count: number; latest_proposal_id: string | null }
  agent: {
    state: 'idle' | 'running' | 'paused'
    run: RunSummary | null
    last_run: RunSummary | null
    next_run_at: string | null
    verify_window_min: number
    fixture_verify_seconds: number | null
  }
  footer: {
    signals: number
    query_history_days: number
    gemini_tokens_today: number
    scripted: boolean
  }
}

export interface Meta {
  version: string
  mode: 'fixture' | 'live'
  apply_target: string
  environment: { id: string; name: string }
  user: { name: string; role: string }
  awaiting_count: number
  chat_suggestions: string[]
}

export interface SettingsValues {
  user_name: string
  user_role: string
  rates_eur_per_gib: Record<'logs' | 'spans' | 'metrics' | 'events', number>
  gemini_usd_per_m_tokens: { input: number | null; output: number | null }
  verify_window_min: number
  schedule_every_hours: number
  agent_paused: boolean
  min_savings_eur_month: number
  fixture_verify_seconds: number
  fixture_fail_next_verification: boolean
}

export interface SettingsResponse {
  values: SettingsValues
  mode: Meta['mode']
  apply_target: string
}

export interface Guardrail {
  id: string
  builtin: boolean
  name: string
  reason: string
  rule: string | null
  service: string | null
  kind: SignalKind | null
  key_pattern: string | null
  created_by: string | null
  created_at: string
  matches: {
    id: string
    service: string
    key: string
    kind: SignalKind
    gib_day: number
    cost_month: number
  }[]
}

export interface Connection {
  id: string
  name: string
  status: 'fixture' | 'connected' | 'not_configured' | 'dry_run'
  summary: string
  description: string
  fields: { label: string; value: string }[]
  note: string | null
}

// Streaming chat events (POST /api/chat, text/event-stream).
export interface ToolCall {
  tool: string
  label: string
  duration_ms: number
  rows: number | null
  result: Record<string, unknown>
}

export type ChatEvent =
  | ({ type: 'tool' } & ToolCall)
  | { type: 'token'; text: string }
  | { type: 'proposal'; proposal: ProposalSummary }
  | { type: 'suggestions'; items: string[] }
  | { type: 'done'; scripted: boolean }
