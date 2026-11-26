# Phase 0 — Kickoff: verify, plan, demo world, skeleton

You are the lead engineer on Sift. First read `AGENTS.md` and `DESIGN.md` completely.
Do not write application code in this phase.

## Step 1 — Verify facts
Use official documentation only (docs.dynatrace.com, github.com/dynatrace-oss, Google Cloud docs,
opentelemetry.io). Confirm or correct each item and cite the URL:

1. **Grail query API** from Python: endpoint, auth (platform token vs OAuth client), request and
   response shape, polling for long-running queries, query cost / limits.
2. **What is ingested**:
   - DQL for volume per signal (logs by source/service/level, spans by service+name, metric
     series cardinality per dimension), and how to get bytes, not only record counts,
   - `dt.system.events` with `event.kind == "BILLING_USAGE_EVENT"`: fields and permissions.
3. **What is used**:
   - `dt.system.query_executions`: schema (query text, user, app context, scanned bytes, time),
     retention, permissions,
   - Documents API: list dashboards and notebooks and read the DQL inside them,
   - where DQL or metric selectors live in workflows, alerting / anomaly detectors and SLOs, and
     how to read them.
4. **OpenPipeline**:
   - how to read and write processors through the API: schema id or endpoint, token scopes, and
     the processor types for drop, field removal, sampling and metric extraction,
   - whether a processor matcher can be evaluated as a DQL filter for a dry run.
5. **Remote Dynatrace MCP server**: endpoint format, auth, and the current tool list.
6. **OpenTelemetry Collector**: `filter`, `transform`, `attributes`, `probabilistic_sampler`,
   `tail_sampling` processors, `otelcol validate`, and OTLP/HTTP export to Dynatrace (endpoint,
   `Api-Token` header, delta temporality for metrics).
7. **ADK (`google-adk`, Python)**:
   - function tools and a remote MCP toolset,
   - requiring human confirmation for a tool,
   - streaming run events (to forward as SSE),
   - Vertex AI configuration.
8. **Gemini on Vertex AI** in `GOOGLE_CLOUD_LOCATION`:
   - current model ids for a strong model (`GEMINI_MODEL`) and a fast, cheap one
     (`GEMINI_FAST_MODEL`), with token prices,
   - how to be sure every call bills to `GOOGLE_CLOUD_PROJECT`,
   - how to set a budget alert.
9. **Drain3** (log template mining): license and Python API.

Write `docs/VERIFIED_FACTS.md` as a table: fact → source URL → correction to AGENTS.md, if any.
If AGENTS.md is wrong, fix it and say so.

## Step 2 — Demo world (single source of truth for fixtures, noisegen and the demo)
Write `docs/DEMO_WORLD.md`: a small online shop "noisy-shop".
- Services: frontend, cart, checkout, payment, inventory, email, recommendation.
- Planted cases. For each case write:
  - the exact telemetry it produces,
  - the expected Sift result,
  - its share of total volume.

| # | Planted case | Expected Sift result |
|---|---|---|
| 1 | payment DEBUG logs (~35 % of log volume, never queried) | drop DEBUG, keep INFO+ |
| 2 | frontend `GET /healthz` spans (~25 % of spans) | drop |
| 3 | cart logs the same exception stack 3× per failed request | dedupe / keep first |
| 4 | inventory "heartbeat ok" log every second | `logs_to_metric` |
| 5 | `user.id` as a dimension on `http.server.request.duration` (high cardinality) | `drop_field` on the metric |
| 6 | email-service logs containing customer e-mail addresses | `redact`, NOT drop |
| 7 | recommendation ERROR logs, noisy but errors | guardrail: no drop; agent explains why and suggests fixing the bug |
| 8 | checkout INFO logs used by a dashboard and an alert | protected: no proposal; shows usage awareness |

## Step 3 — Plan
Write `docs/PLAN.md`:
- milestones matching `prompts/01`–`05`, each with an acceptance test,
- top risks with mitigations (trial tenant expiry, missing permissions for system tables,
  OpenPipeline API shape, DQL cost, Gemini quota and credit burn),
- what to cut first if time runs out,
- a rough credit budget (tokens per analysis run, per day of development),
- the accounts, tokens, scopes and GCP APIs I must set up by hand (I will do these myself).

## Step 4 — Skeleton
Create the layout from AGENTS.md:
- `server/` (uv project, FastAPI hello endpoint, pytest),
- `web/` (Vite + React 19 + TS + Tailwind v4 with the tokens from DESIGN.md, one empty page),
- `mcp/`, `noisegen/` (uv projects with a placeholder and a test),
- `infra/docker-compose.yml`, `fixtures/`,
- `LICENSE` (Apache-2.0), `.gitignore`, `.env.example` with every variable,
- `Makefile` with the targets from AGENTS.md (stubs are fine), `README.md` stub with the pitch.

Run `make test` and `npm run build` in `web/` and show that both pass. Then stop and give me a
short summary: what you verified, what you changed in AGENTS.md, and what I must set up by hand.
Do not start Phase 1.
