# Sift

**Agent-first, lightweight telemetry optimizer for Dynatrace.**

A Gemini agent finds telemetry that nobody uses: log sources, noisy log patterns, span names and
metric dimensions. For each one it proposes a precise ingest rule (a Dynatrace OpenPipeline
processor or an OpenTelemetry Collector config), and shows the evidence and the savings.
It applies the rule only after a human approves it, then verifies that nothing broke and
reverts the rule automatically if something did.

> Status: hackathon project, work in progress. Built with Gemini on Vertex AI and Antigravity CLI.

## Why

Telemetry volume is the biggest cost driver in observability. Pipelines can drop data. The
hard part is deciding **which data is safe to drop**. Dynatrace already holds both halves of
the evidence in Grail: what is ingested and what is actually queried. Sift joins the two with
an agent and keeps a human in the loop.

## Run it (fixture mode, offline)

Needs [uv](https://docs.astral.sh/uv/) and Node 22. No Dynatrace tenant, no Gemini calls:
`SIFT_MODE=fixture` replays recorded Grail, Document, Automation, Settings and SLO responses
from `fixtures/dynatrace/` through the same connectors and engines as live mode.

```bash
cd web && npm install && cd ..
make dev            # API on :8000 + web app on http://localhost:5173
make test           # ruff + pytest (server, noisegen), eslint + type check + build
make demo-reset     # put the demo world back to its starting state
make demo           # build the web app and serve app + API on http://localhost:8000
make discover       # run discovery on the recordings, print the top 10 signals by cost
make fixtures-synth # rebuild fixtures/dynatrace from fixtures/demo_world.json
```

In the app: open a proposal, check the evidence, **Apply rule**, and watch it go
applied → verifying → verified (fixture mode verifies after 8 s). **Run analysis** replays a
scripted agent run; **Ask Sift** (⌘K) answers a few scripted questions from the same numbers.

## Run it against Dynatrace (not run yet: needs a tenant and Docker)

Copy `.env.example` to `.env` and fill in `DT_ENV_URL`, `DT_PLATFORM_URL`,
`DT_PLATFORM_TOKEN` and `DT_API_TOKEN` (scopes are listed in `.env.example`), then:

```bash
set -a; . ./.env; set +a
make up             # noisegen -> OTel Collector -> Dynatrace (docker compose, profile dynatrace)
make noisegen-setup # create the demo dashboards/notebooks and run their queries once
make discover-live  # after ~15 min: top 10 signals by cost from the tenant
make fixtures       # record the tenant's responses into fixtures/dynatrace (review the diff!)
```

The collector configs are checked with the release binary of the same version as the image
([otelcol-contrib 0.166.0](https://github.com/open-telemetry/opentelemetry-collector-releases/releases/tag/v0.166.0)):
`DT_ENV_URL=https://x.live.dynatrace.com DT_API_TOKEN=x otelcol-contrib validate --config=infra/otel-collector/dynatrace.yaml`
(same for `debug.yaml`; both pass).

`make up-debug` runs noisegen into a local collector with the `debug` exporter, no tenant
needed. Without Docker, `cd noisegen && uv run noisegen run --exporter console --fast
--seconds 10` prints the telemetry. Every DQL query and its cost is in
[`docs/DQL.md`](docs/DQL.md).

## Repository

- `AGENTS.md`: product and engineering spec (read by the coding agent)
- `DESIGN.md`: UI design rules and tokens
- `prompts/`: phase-by-phase build prompts (00–05)
- `server/`: FastAPI + SQLite API; Dynatrace connectors (`server/connectors`), discovery,
  usage and pattern engines (`server/engines`), scripted agent runs (fixture mode)
- `web/`: React 19 + Vite + Tailwind CSS v4 app
- `noisegen/`: deterministic noisy-shop telemetry with the 8 planted cases (OTel SDK)
- `infra/`: docker compose with the OpenTelemetry Collector (Dynatrace and debug profiles)
- `fixtures/`: `demo_world.json` (the truth), `dynatrace/` (recorded API responses),
  `agent/` (recorded agent output), proposals, scenario, scripted runs, settings
- `docs/`: `DEMO_WORLD.md`, `PLAN.md`, `DQL.md`

## License

[Apache-2.0](LICENSE)
