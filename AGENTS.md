# AGENTS.md — Sift (working name)

Project context and rules for the coding agent (Antigravity CLI or any agent that reads AGENTS.md).
Read this file and `DESIGN.md` completely before doing anything. Phase prompts live in `prompts/`.

## One-liner

**Sift is an agent-first, lightweight telemetry optimizer for Dynatrace.**
A Gemini agent keeps looking for telemetry that nobody uses: log sources, noisy log patterns,
span names, metric dimensions. For each one it proposes a precise ingest rule, either a
Dynatrace OpenPipeline processor or an OpenTelemetry Collector config. It shows the evidence and
the money, applies the rule only after a human approves it, then checks that nothing broke and
reverts the rule automatically if something did.

## Why this exists (the pitch)

- Telemetry volume is the biggest cost driver in observability. Dynatrace bought **Bindplane**
  (closed Apr 2026) to control telemetry pipelines. That gives them the *mechanism* to drop data.
- What is missing is the *decision*: **which data is safe to drop?** Grafana sells exactly this
  as *Adaptive Telemetry*: usage-based recommendations for metrics, logs, traces and profiles.
  Dynatrace has no equivalent.
- Dynatrace already holds both halves of the evidence in Grail:
  - **what is ingested**: the data itself and billing usage events,
  - **what is used**: query executions, dashboards, notebooks, workflows and alerts.
  Sift joins the two with an agent.
- **Agent-first**: the agent does the analysis. The human reviews evidence and approves.
  The UI is an inbox of agent proposals, not a wall of charts.

## Hackathon constraint (the only hard rule)

**Every LLM call uses Gemini, billed to our Google Cloud project that holds the credits.**
- Call Gemini through **Vertex AI** (`google-genai` / ADK with `GOOGLE_GENAI_USE_VERTEXAI=true`,
  project `GOOGLE_CLOUD_PROJECT`), authenticated with Application Default Credentials or a
  service account of that project. No AI Studio keys on other billing, no non-Gemini models.
- Model ids come from env (`GEMINI_MODEL`, `GEMINI_FAST_MODEL`). Never hardcode them.
- Every Gemini call records token usage per agent run. The UI shows it ("Gemini usage this run").
- **Model availability (verified 2026-11-26 on the credits project, global endpoint):**
  - Gemini 4 models (Hydrogen, Neon, Carbon) return `429 RESOURCE_EXHAUSTED`. The project has
    no Standard PayGo usage tier yet (30-day spend $0).
  - `gemini-3.7-flash` and `gemini-3.8-flash` work.
  - Defaults in `.env.example`: `GEMINI_MODEL=gemini-4-hydrogen`,
    `GEMINI_FAST_MODEL=gemini-3.7-flash`.
- **Model fallback is mandatory.** Every Gemini call goes through one client that:
  1. tries the requested model on the **global** endpoint with truncated exponential backoff
     (max 3 attempts, about 20 s total) on 429 and 5xx,
  2. then falls back to `GEMINI_FAST_MODEL`,
  3. records which model actually served the call in the run log.
  The UI shows the serving model in the run details. A demo must never fail because of 429.

## Design choices (keep unless there is a strong reason)

- **Lightweight**: one `docker compose up`. One Python service, the React app served as static
  files, SQLite. No Kafka, Postgres, Redis or Kubernetes.
- **Dynatrace-first, standards-based**: read through the Dynatrace MCP server and the Grail query
  API. Write OpenPipeline rules (Dynatrace-native) or OpenTelemetry Collector config
  (vendor-neutral and Bindplane-compatible).
- **Safe by default**: nothing is applied without human approval. Every rule has:
  - a dry-run estimate,
  - a verification window,
  - a stored previous state for revert.
  Protected data is never proposed for dropping (see Guardrails).
- **Deterministic first, LLM second**: volumes, usage counts and savings are computed by code.
  Gemini reasons, explains, labels log patterns and drafts rules. Code validates every draft.
- **UI follows `DESIGN.md` exactly.** It describes a calm, dense, monochrome B2B UI with one accent.
- Open source, **Apache-2.0 `LICENSE`** at repo root.

## Repo layout

```
/server     Python 3.12 (uv): FastAPI API, ADK agent, engines, SQLite (SQLModel)
/web        React 19 + Vite + TypeScript + Tailwind CSS v4 (see DESIGN.md)
/mcp        Sift MCP server: thin layer over the server API (the free tool)
/noisegen   deterministic telemetry generator with planted waste → OTel Collector → Dynatrace
/infra      docker-compose.yml, otel-collector config
/fixtures   recorded Dynatrace responses for offline mode (SIFT_MODE=fixture)
/docs       VERIFIED_FACTS.md, PLAN.md, DQL.md, DEMO_SCRIPT.md, PITCH.md
```

## Domain model

- **Signal**: one unit of telemetry Sift can act on.
  - Kinds: `log_source` (service + source + level), `log_pattern` (template from clustering),
    `span` (span name per service), `span_attribute`, `metric`, `metric_dimension`.
  - Fields: volume bytes/day, records/day, estimated cost/month, first/last seen.
- **Usage**: a reference to a signal from:
  - query executions (who, when, app context),
  - a dashboard, notebook or workflow,
  - an alert / anomaly detector or SLO.
  Fields: last used, count in the last 30 days.
- **Finding**: the agent's assessment of a signal.
  - Classes: `unused`, `noise` (health checks, heartbeats, debug), `duplicate`,
    `high_cardinality`, `pii_risk`, `over_verbose_level`.
  - Also: confidence and an evidence list.
- **Proposal**: a concrete action.
  - Actions: `drop`, `sample(rate)`, `drop_field`, `logs_to_metric` (aggregate instead of store),
    `lower_level`, `redact`.
  - Contents:
    - the rule artifact (OpenPipeline processor JSON and/or OTel Collector YAML),
    - estimated savings (GiB/month and €/month),
    - risk and blast radius (everything that references the data),
    - the agent's plain-language explanation.
  - Status: `draft → awaiting_approval → approved → applied → verifying → verified`.
    Side exits: `rejected` or `reverted`.
- **Run**: one agent run (`discover`, `analyze`, `verify`) with steps, tool calls and token usage.
- **AuditEvent**: every state change with actor (`agent` or a user), before/after, timestamp.

## Engines (server)

1. **Discover**: volume and records per signal from Grail (DQL); cost from billing usage events
   and the user's own rates (settings). Never present our estimate as an official Dynatrace price.
2. **Usage**: collect the DQL text from query executions (last 30 days), dashboards, notebooks,
   workflows and alerting config. Extract what each query references: log sources, fields,
   metric keys, span names, filters.
   - A deterministic parser comes first.
   - Gemini (`GEMINI_FAST_MODEL`) handles only queries the parser cannot resolve, and caches the
     result.
3. **Patterns**: cluster log lines into templates (Drain3). Gemini labels templates
   (health check, heartbeat, debug dump, duplicate stack trace, PII-bearing).
4. **Analyze (agent)**: combine volume, usage and patterns into findings and proposals.
   Savings are computed by code. Each draft rule is validated:
   - OTel YAML: `otelcol validate`.
   - OpenPipeline: run the matcher as a DQL filter over the last 24 h to count the records it
     would hit. This count is the **dry run**.
5. **Apply**: OpenPipeline through the Dynatrace configuration API, or write OTel Collector config
   and reload. Store the previous state first. `SIFT_APPLY_TARGET=dry-run` never writes.
6. **Verify** (after the verification window), checking three things:
   - Did volume drop as predicted?
   - Does every query, dashboard and alert that referenced the affected data still return data?
   - Are there no new Davis problems?
   If any check fails: automatic revert and a short incident note.

## Agent (ADK + Gemini)

- Tools:
  - the **Dynatrace MCP server** (DQL, problems, entities),
  - Sift's own tools: `get_signals`, `get_usage`, `get_patterns`, `estimate_savings`,
    `draft_rule`, `validate_rule`, `create_proposal`, `apply_proposal` (requires human approval),
    `revert_proposal`, `verify_proposal`.
- Every number in an agent answer must come from a tool result in the same run.
  A test checks this on fixture runs.
- **Guardrails**: never propose dropping or sampling these:
  - ERROR/FATAL logs,
  - security or audit logs,
  - anything referenced by an alert, SLO or workflow in the last 90 days,
  - anything on the protected list in settings.
- **Chat**: the user can ask, for example, "why is this safe?", "what do checkout logs cost?" or
  "drop debug logs from payment but keep errors". The agent answers with evidence or creates
  proposals. It never applies them itself.
- Runs stream their steps (SSE) so the UI shows what the agent is doing in real time.

## Server API (FastAPI, JSON)

```
GET  /api/overview
GET  /api/signals?kind=&service=&usage=&q=
GET  /api/signals/{id}
GET  /api/proposals?status=
GET  /api/proposals/{id}
POST /api/proposals/{id}/approve | /reject | /revert
POST /api/runs                       {type: discover|analyze|verify}
GET  /api/runs/{id}/events           (SSE)
POST /api/chat                       (SSE)
GET  /api/activity
GET  /api/settings
PUT  /api/settings
```

## MCP server (/mcp): the free tool

Tools:
- `telemetry_waste_report(window_days)`
- `list_proposals(status)`
- `explain_proposal(id)`
- `propose(instruction)`: creates a **draft** proposal and never applies it
- `proposal_status(id)`

Approval always stays with a human in the UI. Must work from Antigravity CLI, Gemini CLI,
Claude Code and Cursor. Use the official `mcp` Python SDK (FastMCP), stdio and streamable HTTP.

## Environment variables (keep `.env.example` in sync)

```
GOOGLE_CLOUD_PROJECT=  GOOGLE_CLOUD_LOCATION=  GOOGLE_GENAI_USE_VERTEXAI=true
GEMINI_MODEL=          GEMINI_FAST_MODEL=
DT_ENV_URL=            # https://<env-id>.live.dynatrace.com   (OTLP ingest, classic APIs)
DT_PLATFORM_URL=       # https://<env-id>.apps.dynatrace.com   (Grail query, documents, settings)
DT_PLATFORM_TOKEN=     # read: storage/query/documents/settings; write: OpenPipeline config (verify scopes)
DT_API_TOKEN=          # OTLP ingest for noisegen
DT_MCP_URL=            # remote Dynatrace MCP server
SIFT_MODE=fixture      # fixture | live
SIFT_APPLY_TARGET=dry-run   # dry-run | openpipeline | otelcol
SIFT_DB=./data/sift.db
SIFT_VERIFY_WINDOW_MIN=30
```

## Engineering rules

- Python 3.12 with `uv`, `ruff`, `pytest`, type hints, small modules.
  TypeScript strict with `eslint`.
- One `Makefile`: `make dev`, `make test`, `make up`, `make demo`, `make demo-reset`, `make fixtures`.
- Every external system (Dynatrace, Vertex AI, OTel Collector) sits behind a small interface with
  a fixture implementation. **`SIFT_MODE=fixture` must make the whole app, UI included,
  demo-able offline** with no tenant and no Gemini calls (recorded agent transcripts).
- Secrets only via env vars. Never commit `.env`, tokens or service-account keys.
- **Do not invent** Dynatrace API endpoints, DQL tables or fields, token scopes, OpenPipeline
  schema or MCP tool names. Look them up in official sources (docs.dynatrace.com,
  github.com/dynatrace-oss, the MCP server's own `tools/list`). If unsure, write
  `TODO(verify):` with the doc link and use the fixture path.
- Before saying something works, run it and put the exact command in the README.
- Keep `docs/PLAN.md` current: done / in progress / cut.
- **This repository is public on GitHub.** Before every commit:
  - run `git status`,
  - make sure nothing from `.env`, `_archive/`, `data/` or any token, key or tenant id is staged.
  When a phase passes its acceptance tests, commit with a clear message and push to `origin main`.
