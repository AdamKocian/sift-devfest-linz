# Phase 5 — Wire the UI to the server, demo polish, pitch

Read `AGENTS.md`, `DESIGN.md` and `docs/PLAN.md` first.

## Build
1. Replace the web mock adapter with the real API.
   - Keep `SIFT_MODE=fixture` on the server as the offline path; the UI does not know the
     difference.
   - SSE for run events and chat. Optimistic UI for approve and reject, with rollback on error.
2. The server serves the built web app as static files. `docker compose up` brings up the whole
   product on one port.
3. Demo controls (hidden behind `?demo=1`):
   - "Reset demo" (clears the DB, reloads fixtures),
   - "Start noisegen",
   - "Verify now" (skips the wait).
4. Polish pass against DESIGN.md: spacing, empty states, loading states (one-line skeletons, no
   shimmer), error states in plain language, keyboard (⌘K, Esc, Enter to approve in the dialog),
   focus rings, dark mode.
5. `README.md` (repo root):
   - problem → why now (Bindplane bought the mechanism, nobody owns the decision; Grafana has
     Adaptive Telemetry, Dynatrace does not),
   - what Sift does → screenshots → architecture diagram (Mermaid),
   - 5-minute quick start in fixture mode → connecting a Dynatrace tenant and Vertex AI,
   - the MCP server, guardrails and safety model, limitations, roadmap.
6. `docs/DEMO_SCRIPT.md`: a 3-minute live demo with a fallback for every step:
   - 0:00 the problem in one sentence and one number,
   - 0:20 Overview: the waste figure,
   - 0:40 the agent run streaming,
   - 1:10 proposal detail: evidence → explanation → rule → dry run,
   - 1:40 approve on the phone,
   - 2:00 verification: volume drops; the recorded failure auto-reverts,
   - 2:30 the guardrail moment (case 7: "I won't drop error logs, here is why"),
   - 2:45 the MCP server used from Antigravity CLI,
   - 2:55 close.
7. `docs/PITCH.md`: 5–6 slide outline for Dynatrace people:
   - the gap between Bindplane (mechanism) and the decision,
   - what we built,
   - why agent-first and why open standards,
   - how it could ship inside Dynatrace (OpenPipeline, Davis, the MCP server),
   - what's next.

## Acceptance
- From a clean clone with only `.env` filled: `docker compose up` → open the URL → the full demo
  works in fixture mode with no network access to Dynatrace or Vertex AI.
- `make test` green, `ruff` and `eslint` clean.
- No secrets in git history: show the output of `git log -p | grep -iE "token|api[_-]?key"`.
- LICENSE detected by GitHub (Apache-2.0 at root).

Stop with: the URL, the list of manual steps left, and anything not verified.
