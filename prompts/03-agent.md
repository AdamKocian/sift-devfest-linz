# Phase 3 — The Sift agent (ADK + Gemini)

Read `AGENTS.md`, `docs/VERIFIED_FACTS.md`, `docs/DEMO_WORLD.md` and `docs/PLAN.md` first.

## Build in `server/agent/`
1. ADK agent on `GEMINI_MODEL` (Vertex AI) with:
   - the remote **Dynatrace MCP server** toolset (`DT_MCP_URL`) for ad-hoc DQL, problems and
     entities,
   - Sift function tools from AGENTS.md: `get_signals`, `get_usage`, `get_patterns`,
     `estimate_savings`, `draft_rule`, `validate_rule`, `create_proposal`, and `apply_proposal`
     (registered but **requires human confirmation**; in this phase it only returns
     "awaiting approval").
2. **Analyze run** (`POST /api/runs {type: "analyze"}`):
   - walk the signals by cost and create findings,
   - for each actionable finding, draft a rule in both targets (OpenPipeline JSON and OTel
     Collector YAML),
   - validate each rule: the OTel YAML with `otelcol validate`, the OpenPipeline matcher as a DQL
     dry run over 24 h,
   - compute savings in code and write a proposal with a 3–5 sentence explanation.
   Unresolved queries from Phase 2 go to `GEMINI_FAST_MODEL` here.
3. **Guardrails in code, not only in the prompt**: `create_proposal` rejects:
   - ERROR/FATAL logs,
   - security/audit logs,
   - anything referenced by an alert, SLO or workflow in 90 days,
   - anything on the protected list.
   Each rejection is returned to the agent with the reason. Case 7 and case 8 from DEMO_WORLD
   must end with no proposal and a clear explanation in the run log.
4. **Streaming**: forward ADK events as SSE on `GET /api/runs/{id}/events`. Steps are written in
   plain words ("Reading 30 days of query history"); tool calls carry duration and row counts.
   Record token usage and estimated Gemini cost per run.
5. **Chat** (`POST /api/chat`, SSE): questions about signals, proposals and cost; instructions
   ("drop debug logs from payment but keep errors") create proposals through the same tools and
   guardrails.
6. **No invented numbers**: a checker compares every number in the agent's final texts with the
   tool results of the same run. Fails the test if a number has no source.
7. Fixture mode: record two real runs into `fixtures/agent/` and replay them deterministically
   with no Gemini calls.

## Acceptance (run these, show output)
- `make test` passes offline:
  - the fixture analyze run yields proposals for cases 1–6 with the expected actions,
  - no proposal for cases 7–8,
  - the number checker passes.
- With a tenant and Vertex AI: one live analyze run. Show the run log, the proposals, and the
  token usage and cost of the run. List the Dynatrace MCP tools the agent actually called.

Update `docs/PLAN.md` and stop with a short summary.
