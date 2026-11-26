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
`SIFT_MODE=fixture` serves the "noisy-shop" demo world from `fixtures/`.

```bash
cd web && npm install && cd ..
make dev            # API on :8000 + web app on http://localhost:5173
make test           # ruff + pytest, eslint + type check + build
make demo-reset     # put the demo world back to its starting state
make demo           # build the web app and serve app + API on http://localhost:8000
```

In the app: open a proposal, check the evidence, **Apply rule**, and watch it go
applied → verifying → verified (fixture mode verifies after 8 s). **Run analysis** replays a
scripted agent run; **Ask Sift** (⌘K) answers a few scripted questions from the same numbers.

## Repository

- `AGENTS.md`: product and engineering spec (read by the coding agent)
- `DESIGN.md`: UI design rules and tokens
- `prompts/`: phase-by-phase build prompts (00–05)
- `server/`: FastAPI + SQLite API, deterministic engine, scripted agent runs (fixture mode)
- `web/`: React 19 + Vite + Tailwind CSS v4 app
- `fixtures/`: the demo world (signals, references, proposals, scenario, scripted runs)
- `docs/`: `DEMO_WORLD.md`, `PLAN.md`

## License

[Apache-2.0](LICENSE)
