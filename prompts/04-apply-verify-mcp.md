# Phase 4 — Approve, apply, verify, revert + the Sift MCP server

Read `AGENTS.md`, `docs/VERIFIED_FACTS.md` and `docs/PLAN.md` first.

## Build
1. **Approval**: `POST /api/proposals/{id}/approve` is the only path to apply. It records the
   approving user, the rule version and an AuditEvent. `reject` and `revert` likewise.
2. **Apply targets** (`SIFT_APPLY_TARGET`):
   - `dry-run`: never writes; logs what would change. This is the default.
   - `otelcol`: writes the processor into the collector config managed by Sift, validates it,
     and reloads the collector.
   - `openpipeline`: reads the current pipeline config, stores it as the revert snapshot, then
     writes the new processor through the API from VERIFIED_FACTS.
     Idempotent: one processor per proposal, with the proposal id in its id or description.
3. **Verify job** after `SIFT_VERIFY_WINDOW_MIN`:
   - volume of the affected signal vs prediction (pass if at least 70 % of the predicted drop),
   - re-run every query, dashboard tile and alert query that referenced the affected data and
     check it still returns data,
   - check for new Davis problems through the Dynatrace MCP server.
   On failure: automatic revert from the snapshot, status `reverted`, and an incident note
   written by the agent (what failed, evidence, next step).
4. **Demo shortcut**: `SIFT_VERIFY_WINDOW_MIN` can be 2 for live demos. In fixture mode,
   verification replays recorded results, including one recorded failure → auto-revert.
5. **`mcp/` Sift MCP server** (official `mcp` SDK, FastMCP, stdio + streamable HTTP), calling the
   server API:
   - `telemetry_waste_report(window_days)`
   - `list_proposals(status)`
   - `explain_proposal(id)`
   - `propose(instruction)`: creates a draft only
   - `proposal_status(id)`
   Tool descriptions are written for an LLM. No tool can approve or apply.
   Add a README section with config snippets for Antigravity CLI, Gemini CLI, Claude Code and
   Cursor.

## Acceptance (run these, show output)
- `make test` passes offline: approve → apply (dry-run and otelcol) → verify pass; and the
  recorded failure → auto-revert with the snapshot restored.
- With a tenant: apply case 1 through `otelcol` or `openpipeline`, wait for verification, and
  show the volume before and after. Show the AuditEvents.
- The MCP server lists its tools in the MCP Inspector (or a small client script), and
  `telemetry_waste_report` returns the planted cases.

Update `docs/PLAN.md` and stop with a short summary.
