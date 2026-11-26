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

## Repository

- `AGENTS.md`: product and engineering spec (read by the coding agent)
- `DESIGN.md`: UI design rules and tokens
- `prompts/`: phase-by-phase build prompts (00–05)

## License

[Apache-2.0](LICENSE)
