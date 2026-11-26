# Phase 2 — Noise generator, Dynatrace connectors, discovery and usage engines

Read `AGENTS.md`, `docs/VERIFIED_FACTS.md`, `docs/DEMO_WORLD.md` and `docs/PLAN.md` first.

## Build
1. `noisegen/`: produce the telemetry of every planted case in `docs/DEMO_WORLD.md` with the
   OpenTelemetry SDK (logs, spans, metrics), with a fixed seed and a configurable rate.
   - Keep default volume small: enough for the demo, not enough to burn trial ingest.
   - Also create, through the API, the dashboard + alert that make case 8 "used", and a few
     realistic query executions (run the DQL a dashboard would run), so usage data exists.
2. `infra/`: docker-compose with `otel-collector` (contrib) → OTLP/HTTP → Dynatrace, plus a
   `debug` exporter profile for work without a tenant.
3. `server/` connectors, each behind an interface with a `Fixture` implementation:
   - `GrailClient` (DQL query API with polling, timeouts, and a cost guard: max scanned bytes and
     a query timeframe limit),
   - `DocumentsClient` (dashboards, notebooks),
   - `ConfigClient` (workflows, alerting, SLOs: read only in this phase),
   - `DynatraceMcp` (remote MCP client, used by the agent in Phase 3; here just list tools).
4. Engines (pure functions over connector data, unit-tested):
   - **Discover**: signals with volume/day, records/day, cost/month (rates from settings).
   - **Usage**: parse DQL from query executions, dashboards, notebooks, workflows and alerts.
     Extract referenced log sources, fields, metric keys and span names. Deterministic parser
     first; mark unresolved queries for Gemini (Phase 3). Use `GEMINI_FAST_MODEL` only through
     an interface that is mocked in tests.
   - **Patterns**: Drain3 templates for high-volume log sources, with example lines (redacted).
5. SQLite schema (SQLModel) for Signal, Usage, Finding, Proposal, Run, AuditEvent.
   Endpoints `GET /api/overview`, `/api/signals`, `/api/signals/{id}`, `/api/activity`.
6. `make fixtures`: record live responses into `fixtures/` (redact tenant ids and tokens).
   `SIFT_MODE=fixture` replays them.
7. Document every DQL query in `docs/DQL.md` with what it is for and its typical scanned bytes.

## Acceptance (run these, show output)
- `make test` passes offline. Engine tests prove that every planted case from DEMO_WORLD is
  detected with the right usage status (case 8 = used, cases 1–5 = unused).
- With a tenant: `make up` + noisegen for 15 minutes, then `GET /api/signals` returns the
  planted cases with real numbers. Show the top 10 signals by cost.
- `docs/DQL.md` exists and every query in it was run at least once (or is marked `TODO(verify)`).

Update `docs/PLAN.md` and stop with a short summary.
