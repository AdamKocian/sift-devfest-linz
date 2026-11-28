# Plan — Sift

> **Status 2026-11-28:** Phase 0 reviewed, phase 1 done, phase 2 done offline, phase 3
> done without a tenant, **phase 4 done offline** (apply targets, verification with automatic
> revert, Sift MCP server; tenant acceptance pending, GCP billing currently disabled). Next:
> phase 5 (integration and a demo for a Dynatrace engineer; no pitch).
>
> **Review 2026-11-26 (after the Gemini 3.7 Flash run of phase 00):** `docs/VERIFIED_FACTS.md` was removed (sources were generic doc root URLs; several claims contradicted official docs, e.g. query executions live in `dt.system.query_executions`). The `mcp/`, `noisegen/` and `infra/` stubs returned hard-coded numbers and were removed; they are rebuilt in phases 02 and 04. Facts must be re-verified with real source links before they are relied on.


This document outlines the milestones, acceptance criteria, risk mitigations, development budget, and manual prerequisite setup required for the Sift project.

---

## 1. Milestones & Acceptance Tests

### Phase 0 — Kickoff: Verification, Demo World & Skeleton (done, reviewed)
- **Goal:** Verify technical facts, define demo world, structure repo, create passing skeleton.
- **Acceptance Test:**
  - `make test` runs pytest across `server/`, `mcp/`, `noisegen/` with 100% passing tests.
  - `npm run build` in `web/` completes cleanly with 0 TypeScript/CSS build errors.
  - `docs/VERIFIED_FACTS.md`, `docs/DEMO_WORLD.md`, and `docs/PLAN.md` exist and are consistent.

### Phase 1 — UI on Fixtures (`prompts/01-ui-on-fixtures.md`) — **done 2026-11-26**
- **Goal:** Build the complete React 19 + Tailwind CSS v4 UI following `DESIGN.md` in `SIFT_MODE=fixture`.
- **Done:**
  - Server (`server/`): SQLite via SQLModel; every endpoint from AGENTS.md plus `GET /api/meta`,
    `/api/runs`, `/api/runs/{id}`, guardrails CRUD, `/api/connections`, `POST /api/demo/reset`.
    Deterministic engine (`engine.py`) computes volume, cost, usage, guardrails, savings and dry
    runs from the fixture rows; nothing is hard-coded in the API.
  - Proposal lifecycle approve → applied → verifying → verified, reject, revert, automatic revert
    on failed verification (fixture: Settings > Verification > "Fail the next verification").
    Every change is an `AuditEvent`. Guardrails block applying protected data.
  - Scripted agent runs (analyze / discover / verify) and Ask Sift chat stream over SSE. No
    Gemini calls in fixture mode; every number in a run summary or chat answer comes from a tool
    result of that run (tested).
  - Fixtures rewritten from `docs/DEMO_WORLD.md` (36 signals, 31 references, 8 proposals across
    all statuses); `fixtures/scenario.json` seeds the history by replaying it through the API code.
  - Web (`web/`): every screen in DESIGN.md (shell, ⌘K palette, environment switcher, mobile
    header + tab bar, Overview, Proposals list + detail with Evidence / Rule / Impact / History,
    Signals list + detail, Activity, run timeline drawer, Ask Sift drawer / mobile tab,
    Connections, Guardrails, Settings), typed client against the real API, light/dark,
    usable at 375 px. ESLint added (also bans hex colours), `tailwind-variants` v3, fonts bundled.
- **Acceptance (passing):** `make test` (ruff + 26 pytest tests, eslint + `tsc` + vite build);
  no hex colours in `web/src/*.tsx`; checked in the browser at 1440×900 (light, dark) and 375×812.
- **Not done / cut:** no screenshots saved to `docs/screenshots/` (checked in the browser pane
  instead); no login; one environment only; OpenPipeline JSON keys in `fixtures/proposals.json`
  are marked `TODO(verify)`; OTel configs not run through `otelcol validate` (no collector yet).

### Phase 2 — Telemetry & Engines (`prompts/02-telemetry-and-engines.md`) — **done offline 2026-11-26**
- **Done:**
  - Connectors (`server/server/connectors/`), each with a live and a fixture implementation
    sharing one parse function: `GrailClient` (execute + poll, timeout with cancel, retries
    on 429/5xx, `CostGuard`: `from:` required, ≤ 90 d, no lifted scan limit, 200 GB per run),
    `DocumentsClient` (paged list, multipart content), `ConfigClient` (workflows, custom
    alerts, SLOs; read only), `DynatraceMcp` (remote MCP, `tools/list` only).
    `RecordingGrailClient` + `server/record.py` for `make fixtures` (redacts env id, tokens,
    e-mails, log content).
  - Engines (`server/server/engines/`, pure functions): **discover** (volume/records per
    day, growth, first seen; size estimated from content length / span attributes / series
    and calibrated to billed bytes), **patterns** (Drain3 on sampled lines, dominant-template
    rule, redacted examples, exact pattern volume via `matchesPhrase`), **usage**
    (deterministic DQL parser: filters as DNF, fields, metric keys, dashboard variables;
    conservative attribution; unresolved queries go to a `QueryResolver`; recorded Gemini
    answers in fixture mode, mock in tests), **cost** (all-in default; split ingest/retain
    model with savings per apply target, off until the user decides).
  - Phase-1 `engine.py` now runs on the engines' output: fixture seeding calls
    `pipeline.run` on Grail/Documents-shaped recordings (`fixtures/dynatrace/`), synthesised
    from `fixtures/demo_world.json` by `make fixtures-synth`. `fixtures/signals.json` is gone;
    signal ids are derived from the data (`log-payment-app-debug`, ...). Demo numbers are
    unchanged (€3,573/month, €1,321 awaiting, €369 saved).
  - `noisegen/` (OTel SDK, fixed seed, `--scale`, all 8 planted cases, delta metrics,
    `setup` creates the demo documents and runs their queries) and `infra/` (compose with
    otel-collector-contrib 0.166.0 → OTLP/HTTP → Dynatrace, `debug` profile).
  - `docs/DQL.md`: every query, purpose, scanned-bytes estimate, sources.
- **Acceptance (offline, passing):** `make test` = ruff + 76 server tests (engine tests prove
  the 8 planted cases: 1, 2, 4, 5 unused; 3 rarely = 2 ad-hoc queries, no dashboard/alert;
  6 used by a notebook, so redact only; 7 ERROR guardrail; 8 used and protected), ruff + 4
  noisegen tests (in-memory exporters), eslint + build. App checked in the browser pane.
- **Pending (needs a tenant / Docker):**
  - `make up` + 15 min noisegen, then `make discover-live` → top-10 signals by cost with
    real numbers. Not run: no Dynatrace tenant, Docker/OrbStack not installed.
  - Every query in `docs/DQL.md` is `TODO(verify)` until run once.
  - Collector configs: `otelcol-contrib validate` (v0.166.0 release binary, sha256 checked
    against the GitHub asset digest) passes for `infra/otel-collector/dynatrace.yaml` and
    `debug.yaml`. Ingest into a tenant not tested.
  - `make fixtures` (record) written, not run.
- **TODO(verify) list:** `dt.system.query_executions` schema; Document list response
  fields and multipart part names; document create fields; custom-alert settings schema id
  and value fields; SLO `customSli.indicator`; workflow DQL action id; `metrics` command
  with `from:`; span field names; classic token scopes for logs/metrics ingest; whether
  sampling lowers billed scans; `client.source` of SLO evaluations.
- **Cut / changed:** the API server stays fixture-only (live discovery is a CLI:
  `make discover-live`); live API mode arrives with the agent in phase 3. Spans and metrics
  have no sample records (logs do). Metric first-seen is the 7-day window start (the
  `metrics` command sees 10 days). Ad-hoc query references are named by app
  ("Ad-hoc query · Logs"), not by notebook title.
- **Decision 2026-11-26 (user, via sift-otazky):** keep the all-in €/GiB rates for the demo
  ("Zatiaľ jedna cena"). The split ingest/retain model stays in code, switched off; demo
  numbers unchanged. Revisit in phase 4 when apply targets are real (docs/DISCOVERY.md §4).

### Phase 3 — Agent (`prompts/03-agent.md`) — **done without a tenant 2026-11-27**
- **Done** (`server/server/agent/`):
  - `gemini.py`: the one Gemini client for every call (agent, Ask Sift, usage resolver).
    Vertex AI (`genai.Client(vertexai=True)`), location `global`, project and model ids from
    env. Truncated exponential backoff (3 attempts, ~20 s, SDK retries off) on 408/429/5xx
    and network errors, then `GEMINI_FAST_MODEL`; the rest of the run stays on the fast
    model and other runs skip the failed model for 5 min. Every call records requested and
    serving model, attempts, tokens (thinking = output, cached input) and estimated cost
    from the dated price list in `pricing.py` (source: cloud.google.com/model-garden/pricing,
    read 2026-11-26). Token budget per run (`SIFT_GEMINI_MAX_TOKENS_PER_RUN`).
  - `llm.py`: `SiftGemini`, an ADK `BaseLlm` on that client. `runner.py`: ADK `LlmAgent`
    (`static_instruction`, thinking level LOW) with Sift's tools as functions, `Runner` +
    `InMemorySessionService`, `RunConfig(max_llm_calls)`; Dynatrace `McpToolset`
    (streamable HTTP, Bearer token) only when `DT_MCP_URL` is set. Run log by phase
    (plain-word steps, tool rows with duration and rows), proposals, Gemini calls.
  - `tools.py`: get_signals, get_usage, get_patterns, record_finding, draft_rule,
    validate_rule, estimate_savings, create_proposal, get_proposals, apply_proposal (only
    reports "awaiting approval"). Guardrails in code in `create_proposal` (ERROR/FATAL,
    audit, alert/SLO/workflow in 90 d, protected list, also for the parts of a signal),
    plus: no removal of data a dashboard/notebook read in 30 d, no complete removal of
    queried data, one proposal per signal, no re-proposing rejected/reverted rules,
    savings threshold, explanation = 3–5 placeholder templates whose rendered numbers must
    come from tool results. Every rejection goes back to the agent with its reason.
  - `rules.py`: one structured matcher → OpenPipeline processor (DQL matcher) and OTel
    Collector snippet (filter / transform / count connector, OTTL). Collector config
    checked in code; `otelcol validate` only with `SIFT_OTELCOL_BIN` ("not run" otherwise).
    All 10 drafted configs of the test drafts passed `otelcol-contrib validate` v0.166.0
    by hand (release binary from phase 2). OpenPipeline dry run = the matcher as a 24 h DQL
    filter through the `GrailClient` (`dql.dry_run`; metric dimensions re-run
    `metric_cardinality`); fixture recordings for agent drafts are answered from the demo
    world by `server.synth` while recording, and kept by `make fixtures-synth`.
  - `checker.py`: number checker. A run's closing text with a number that is in no tool
    result of the run is not shown; code writes the summary from `summarize_run` instead.
  - Record/replay: `make agent-record` records a real run (`fixtures/agent/analyze.json`,
    clean world = discovery only). Fixture mode (and seeding) replays it through the same
    tools: if every result matches, the agent's texts are shown; on changed data code
    writes the summary. No Gemini call. The demo's "14 min ago" analysis is that replay
    (cases 4/5 already verified, static spans rejected) and keeps the demo numbers
    (4 awaiting, €1,321).
  - Ask Sift: `SIFT_AGENT=gemini` → live agent with the same tools (SSE: tool rows, text,
    proposals, usage). Fixture mode replays `fixtures/agent/chat.json` when the question
    matches and the data is unchanged, else the scripted answers.
  - `resolver.py`: `GeminiResolver` (fast model, JSON schema) for queries the DQL parser
    cannot read, cached in the `usage_resolutions.json` shape (`make resolver-check`).
  - Config: `SIFT_AGENT=replay|gemini`; DB columns `run.cost_usd`, `llm_calls`, `replay_of`
    added to existing databases by `db.add_missing_columns`. Run drawer shows replay note
    and call count.
- **Acceptance (offline, passing):** `make test` = ruff + 103 server tests (incl.
  `test_gemini.py`: simulated 429 → backoff 3 s, 6 s → fallback, sticky fallback, budget;
  `test_agent.py`: cases 1–6 pass every check with the expected action, 7 and 8 blocked by
  guardrails, usage/duplicate/threshold/text rejections, the recorded run replayed offline
  with no Gemini call → proposals for cases 1–6, none for 7–8, the run log names their
  guardrails, every agent text's numbers sourced; `test_agent_loop.py`: the ADK loop with a
  fake Vertex transport, 429 on the main model inside the loop), noisegen, eslint + build.
  `make agent-replay` passes. App checked in the browser pane (Overview, run drawer with
  replay, proposal detail + rule, Run analysis, Ask Sift; no console errors).
- **Real Vertex AI runs (no tenant):** three analysis runs on 2026-11-26, 8 Gemini calls
  each: (1) gemini-4-hydrogen for 5 calls, then 429 → gemini-3.7-flash; 79.9k input (47.1k
  cached) + 3.8k output tokens, $0.062; the number checker rejected its closing text
  (it added up €1,618 itself) → `get_proposals` now returns totals. (2) all on flash
  (Hydrogen 429), $0.025, missed case 3 → prompt now describes duplicates. (3) recorded:
  all on gemini-3.7-flash after Hydrogen 429, 69.8k input (43.4k cached) + 2.4k output,
  **$0.032**; proposals for cases 1–6 (+ unused static spans, legit in the clean world),
  finding "fix the bug" for case 7, nothing for 8. Total spend ≈ $0.12.
- **Pending:**
  - **Open: the second real run, a chat instruction.** Record "Drop debug logs from payment
    but keep errors" with `make agent-chat-record` (writes `fixtures/agent/chat.json`, which
    Ask Sift replays in fixture mode) and run the live `make resolver-check`. Both wait
    because **billing is disabled on the GCP project since 2026-11-27** (403
    BILLING_DISABLED, `billingEnabled: false`). Decision 2026-11-27 (user, via
    sift-otazky): close phase 3 with the one recorded analyze run; record the chat run once
    the user enables billing again. Only the user changes billing.
  - Tenant: the agent's Dynatrace MCP tools have never been called (no `DT_MCP_URL`); the
    live analyze run on a tenant, its run log and the MCP tools it calls are pending.
    `SIFT_MODE=live` for the API server is still not implemented.
  - TODO(verify): OpenPipeline configuration JSON, DPL pattern in the redact processor,
    span field `http.response.status_code`, `dt.system.query_executions`, the dry-run DQL
    on a tenant; Gemini 4 Hydrogen availability (intermittent 429 on this project).
- **Cut:** `sample` and `lower_level` actions are not drafted (need a Collector routing
  step and an unverified OpenPipeline processor); ADK `require_confirmation` is not used
  (approval happens in the UI, `apply_proposal` only reports it).

### Phase 4 — Apply, Verify & MCP Server (`prompts/04-apply-verify-mcp.md`) — **done offline 2026-11-28**
- **Done:**
  - **Approval is the only path to apply** (`services.approve`): the approving person and the
    rule version (`rule_version`, a content hash of both rule targets) are stored; the UI
    sends the version it showed and the approval fails with 409 if the rule changed since.
    Then the apply target reads the current configuration, the snapshot is committed, and only
    then is the rule written. Writers refuse a proposal that is not approved
    (`apply.check_approved`). A failed write leaves nothing behind: AuditEvent `apply_failed`,
    back to awaiting approval. Approve / reject / revert / auto-revert are AuditEvents with
    before/after (target, ids, restore mode). Neither the agent nor the MCP server can approve.
  - **Apply targets** (`server/server/apply/`, one interface: snapshot → write → revert):
    - `dry-run` (default): writes nothing, logs what it would add.
    - `otelcol`: Sift-managed Collector config (`SIFT_OTELCOL_CONFIG`, copied from
      `infra/otel-collector/dynatrace.yaml`); components `<type>/sift-<proposal id>` (filter /
      transform before `batch`; count connector in its own `logs/` + `metrics/` pipelines so it
      counts before the filter); `otelcol validate` before the atomic replace when
      `SIFT_OTELCOL_BIN` is set; reload by SIGHUP (`pid:` or `docker:`; source: `Run` in
      [otelcol/collector.go](https://github.com/open-telemetry/opentelemetry-collector/blob/main/otelcol/collector.go)
      handles SIGHUP with `reloadConfiguration`; the file provider does not watch files).
      A failed validate or reload restores the old file. Idempotent; revert restores the
      snapshot byte for byte, or removes only this proposal's components when someone else
      edited the file since. Compose mounts `data/otelcol/` (`make otelcol-config`).
    - `openpipeline`: Settings 2.0 object of `builtin:openpipeline.{logs,spans,metrics}.pipelines`
      ([schema](https://docs.dynatrace.com/docs/dynatrace-api/environment-api/settings/schemas/builtin-openpipeline-logs-pipelines)),
      pipeline chosen by `customId` (`SIFT_OPENPIPELINE_PIPELINE`); processors `sift-<proposal
      id>[-metric|-storage]` (`drop`, `fieldsRemove{fields}`, `dql{script}`,
      `counterMetric{metricKey,dimensions}`, `noStorage` first in the first-match storage
      stage). `GET /api/v2/settings/objects?schemaIds=&fields=objectId,value,updateToken,schemaVersion,schemaId`
      → PUT `/api/v2/settings/objects/{id}?validateOnly=true` → PUT with `updateToken`
      ([get-objects](https://docs.dynatrace.com/docs/dynatrace-api/environment-api/settings/objects/get-objects),
      [put-object](https://docs.dynatrace.com/docs/dynatrace-api/environment-api/settings/objects/put-object)).
      Revert = snapshot if unchanged, else remove own processors. Fixture mode writes to
      recorded pipeline settings (`fixtures/dynatrace/settings/openpipeline_pipelines.json` →
      `data/fixture-settings/`). The old `/platform/openpipeline/v1/configurations` API is
      deprecated and not used.
  - **Verification** (`server/server/verify.py`) after `SIFT_VERIFY_WINDOW_MIN` (2 for live
    demos): volume of the affected signal before vs after in one scan of 2·W minutes, pass at
    ≥ 70 % of the predicted drop (attribute presence / series with a dimension for
    `drop_field`; records keep arriving and none unmasked for `redact`); every dashboard tile,
    notebook, workflow and alert query next to the data re-run over W minutes (tiles that
    group by a dashboard variable are re-run with the removed field); new Davis problems via
    the Dynatrace MCP server (`execute_dql` on `fetch dt.davis.problems | filter
    not(dt.davis.is_duplicate)`, argument name taken from `tools/list`; Grail as fallback).
    On failure: automatic revert from the snapshot, status `reverted`, incident note (what
    failed, evidence, next step): recorded in fixture mode, Gemini (fast model, number-checked)
    with `SIFT_AGENT=gemini` in live mode, else a template. Fixture mode replays
    `fixtures/verify/<id>.json` (query texts checked against the code; `make fixtures-synth`
    regenerates them), including the recorded failure of `p-inventory-dbstatement`
    ("Inventory DB latency" empty → reverted in the demo's history); proposals without a
    recording use results derived from the demo world; "Fail the next verification" still
    shows an automatic revert live.
  - **Sift MCP server** (`mcp/`, official MCP Python SDK 2.3.0, `MCPServer` = mcp 1's
    `FastMCP`; stdio and streamable HTTP): `telemetry_waste_report(window_days)`,
    `list_proposals(status)`, `explain_proposal(id)`, `propose(instruction)`,
    `proposal_status(id)` over new endpoints `GET /api/report/waste` and
    `POST /api/proposals/propose`. Descriptions written for an LLM; read-only hints; no tool can
    approve, apply or revert. `propose` is deterministic in fixture mode (the agent's own
    tools and guardrails, no Gemini). `mcp/scripts/demo_client.py` shows `tools/list` and
    `telemetry_waste_report` over both transports (`make mcp-demo`). README has config for
    Claude Code, Gemini CLI, Antigravity CLI/IDE and Cursor (formats from each client's docs).
  - UI: approve dialog and Rule tab show the rule version and the target; Rule tab shows what
    was written (file / pipeline, ids, approved version); Verification shows where the results
    came from and who wrote the incident note.
- **Acceptance (offline, passing):** `make test` = ruff + 121 server tests (1 skipped without
  `SIFT_OTELCOL_BIN`; `tests/test_apply.py`: approval-only writes and rule version;
  approve → apply dry-run and otelcol → verify pass; recorded failure → auto-revert with the
  snapshot restored for otelcol and openpipeline; otelcol idempotence, SIGHUP/docker reload,
  restore after failed reload/validate, edits by others survive a revert; OpenPipeline live
  client against a mocked Settings API (paging, validateOnly then write, updateToken); MCP
  evidence path and Grail fallback; incident-note number check; waste report lists the 8
  planted cases; propose creates a proposal and never applies) + 4 noisegen + 3 mcp tests
  (tool list, no approve/apply tool, stdio and streamable HTTP end to end against an offline
  Sift API: `telemetry_waste_report` returns cases 1, 2, 3, 6 awaiting, 4, 5 verified, 7, 8
  protected) + eslint + build. With the otelcol-contrib 0.166.0 release binary from phase 2,
  `SIFT_OTELCOL_BIN=… pytest -k otelcol_validate` passes (managed config with all rules).
  `make agent-replay` still matches the recording. App checked in the browser pane
  (proposal detail, History, Rule, Verification, Activity, 375 px; no console errors).
  Approving in the UI was left to the user (the approval click is a person's action).
- **Pending (tenant / billing):**
  - With a tenant: apply case 1 through `otelcol` or `openpipeline`, wait for verification,
    show volume before/after and the AuditEvents. Not run: no tenant, no Docker.
  - Live incident note and live `propose` with `SIFT_AGENT=gemini`: wait for GCP billing.
  - `SIFT_MODE=live` for the API server is still not implemented (live evidence is wired in
    `services._evidence` and unit-tested with fakes only).
- **TODO(verify):** which host a platform token uses for the Settings API (`.live` per the
  API pages vs `.apps…/platform/classic/environment-api`); which processor types each
  pipeline stage accepts; required keys of a counter-metric dimension entry and whether log
  metric keys need `log.`; the execute_dql output format of the remote MCP server; the
  verification DQL (`countIf` on `timestamp`/`start_time`, `timeseries … to:` + `append`,
  `dt.davis.problems` fields and token scope); classic `docker kill --signal=HUP` container
  name; Davis problems are not yet scoped to the affected service (any new problem fails).
- **Cut:** `sample` and `lower_level` still not drafted; Sift never creates or re-routes
  OpenPipeline pipelines (it edits the one you name).

### Phase 5 — Integrate & Demo (`prompts/05-integrate-demo-pitch.md`)
- **Change 2026-11-28:** there is no pitch. The project is shown to a programmer at
  Dynatrace, so correctness of the Dynatrace parts (real APIs, verified against docs) and an
  offline demo that always works matter most. `docs/PITCH.md` and the pitch narrative are cut.
- **Deliverables:**
  - `make demo` (one process, offline, fixture mode) and `make demo-reset` (also clears what
    fixture mode "wrote": managed Collector config, fixture pipeline settings).
  - `docs/DEMO_SCRIPT.md`: a short walkthrough for a Dynatrace engineer: the inbox, evidence of
    one proposal (DQL and dry run), the rule in OpenPipeline Settings 2.0 and Collector form,
    approve → verify, the recorded auto-revert, the MCP server from an MCP client, and where
    each Dynatrace API is verified (links) or still `TODO(verify)`.
  - With a tenant (if one exists by then): the live acceptance of phases 2–4.
- **Acceptance Test:**
  - `make demo` starts the system offline and the walkthrough completes without a tenant.
  - `make demo-reset` cleanly resets database and fixtures to baseline.

---

## 2. Top Risks & Mitigations

| Risk | Impact | Mitigation Strategy |
|---|---|---|
| **1. Dynatrace Trial Expiry or Outage** | High | Full offline mode (`SIFT_MODE=fixture`) with recorded DQL query responses, system events, and dashboards. The entire application and demo operate 100% offline. |
| **2. Missing IAM Permissions on System Tables** | Medium | Graceful fallback: if `dt.system.events` is inaccessible or restricted, the usage engine defaults to document/dashboard analysis + heuristic estimation and flags permission warnings in Settings. |
| **3. OpenPipeline API Version Changes** | Medium | Settings 2.0 API `/api/v2/settings/objects` schemas `builtin:openpipeline.*` with `SIFT_APPLY_TARGET=dry-run` or `otelcol` as alternative write paths. |
| **4. DQL Query Cost & Rate Limits** | Medium | Ingest queries aggregate data using `summarize by:{...}` over 7-day buckets; results are cached locally in SQLite. Scanned bytes are tracked per run. |
| **5. Gemini 429 Quota & Credit Burn** | High | Multi-tier client: backoff retry on global endpoint → automatic fallback to `GEMINI_FAST_MODEL` (`gemini-3.7-flash`). Token counts tracked and displayed per run. Deterministic code handles all arithmetic and parsing first. |

---

## 3. What to Cut First (If Time Runs Out)

If development time is constrained during the hackathon, features will be trimmed in the following order:
1. **Live OpenPipeline API writing:** Default to `SIFT_APPLY_TARGET=dry-run` and OTel Collector YAML generation (still fully demonstrates value and saves user copy-pasteable rules).
2. **Complex DQL regex AST parser:** Rely primarily on the deterministic string extractor + fast Gemini query analyzer.
3. **Multi-tenant environment switching:** Support single environment ID configured via `.env`.
4. **Custom Davis problem analyzer integration:** Rely on Davis problem query via DQL for verification.

---

## 4. Rough Credit & Token Budget

- **Models:** `GEMINI_MODEL=gemini-4-hydrogen` (answers intermittently, often 429 on this
  project), `GEMINI_FAST_MODEL=gemini-3.7-flash`.
- **Prices** (cloud.google.com/model-garden/pricing, 2026-11-26, global, per 1M tokens):
  3.7/3.8 Flash $0.75 input / $3.75 output / $0.075 cached input until 2026-12-31, then
  $1.50 / $7.50 / $0.15; 4 Hydrogen $1.50 / $7.50 / $0.075 cached. Thinking is billed as
  output. Only 200 responses are charged.
- **Measured per analysis run** (8 Gemini calls, thinking level LOW, tools called in
  parallel batches): 63–80k input tokens of which 43–47k served from the implicit cache,
  2–4k output tokens → **$0.025–0.062 per run**. An Ask Sift answer is smaller (budget
  capped at 200k tokens, 15 calls).
- **Daily development budget (100 runs/day):** ~$3–6 / day at those numbers.
- Total hackathon budget consumption across development and rehearsal is estimated under **$15.00**, well within Google Cloud credits.

---

## 5. Manual Prerequisites Setup (For User)

The following items must be configured in Google Cloud and Dynatrace by the project owner:

### A. Google Cloud / Vertex AI Setup
1. **GCP Project:** Ensure `GOOGLE_CLOUD_PROJECT` is set to the project with hackathon credits.
2. **Enable Vertex AI API:**
   ```bash
   gcloud services enable aiplatform.googleapis.com --project=<GOOGLE_CLOUD_PROJECT>
   ```
3. **Application Default Credentials (ADC):**
   ```bash
   gcloud auth application-default login
   ```
4. **Billing Budget Alert:**
   - In GCP Console → **Billing** → **Budgets & alerts** → Create budget for `<GOOGLE_CLOUD_PROJECT>` with alerts at 50%, 80%, and 100%.

### B. Dynatrace Tenant Setup (For Live Mode)
1. **Dynatrace Environment:** Active Dynatrace SaaS environment URL (e.g. `https://<env-id>.apps.dynatrace.com` and `https://<env-id>.live.dynatrace.com`).
2. **Platform Token (`DT_PLATFORM_TOKEN`):**
   - Platform token scopes Sift reads with (from the API pages linked in the connectors):
     `storage:logs:read`, `storage:spans:read`, `storage:metrics:read`, `storage:events:read`,
     `storage:system:read`, `storage:buckets:read`, `document:documents:read`,
     `automation:workflows:read`, `slo:slos:read`, `settings:objects:read` (custom alerts,
     TODO(verify)); MCP: `mcp-gateway:servers:invoke`, `mcp-gateway:servers:read`.
     `noisegen setup` also needs `document:documents:write`. OpenPipeline target (phase 4):
     `settings:objects:read` and `settings:objects:write` (the PUT page adds that
     `settings.read` is needed as well; host for platform tokens TODO(verify)).
3. **Ingest token (`DT_API_TOKEN`):**
   - Platform token with `openpipeline:traces:ingest`, `openpipeline:metrics:ingest`,
     `openpipeline:logs:ingest`, sent as `Authorization: Bearer` by the collector
     ([collector configuration](https://docs.dynatrace.com/docs/ingest-from/opentelemetry/collector/configuration)).
