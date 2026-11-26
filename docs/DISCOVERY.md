# Discovery — does Sift fill a real gap?

> Checked 2026-11-26 against public docs, release notes and vendor pages only. We have not looked
> inside a Dynatrace tenant, so preview or unannounced features may exist. Re-check before the pitch.

## 1. Does Dynatrace already recommend what to drop based on usage?

**Verdict: the gap is real but narrow. Dynatrace has the parts, not the decision.**

What Dynatrace has (none of it says "this data is unused, drop it"):

| Area | What it does | Usage-based drop advice? |
|---|---|---|
| Optimize docs (cost management) | Two levers: "Send and keep less data" and query more efficiently. Usage dashboards give suggestions such as "queries missing a scan limit" and hosts that could downgrade monitoring mode. [docs](https://docs.dynatrace.com/docs/manage-your-costs/optimize) | No |
| Dynatrace Assist, cost intelligence | Explains spikes from billing usage, forecasts cost, finds "log queries duplicated by multiple users". [Release Radar 06.26](https://www.dynatrace.com/news/blog/dynatrace-release-radar-06-26/), [docs](https://docs.dynatrace.com/docs/dynatrace-intelligence/use-cases/cost-intelligence) | No (query side only) |
| OpenPipeline (2026 releases) | Drop/filter processors, high-cardinality validation for metric processors (2.10.1, May 2026), usage dashboard for forwarding. [Hub release feed](https://hub-manager.hub.central.dynatrace.com/api/v1/public-hub/feed/apps/openpipeline/) | No (mechanism, not decision) |
| Metrics best practices | Manually "drop unused data", e.g. metrics not used in the last year. [docs](https://docs.dynatrace.com/docs/analyze-explore-automate/metrics/best-practices-metrics) | Advice only, no tooling |
| Storage Management app | Buckets, retention, per-bucket query byte statistics. [Hub](https://www.dynatrace.com/hub/detail/storage-management/) | No (visibility only) |
| Bindplane (closed Apr 15 2026) — Global Pipeline Intelligence | AI "Delete Fields" and "Sensitive Data Detection" recommendations built from **data snapshots**, private beta, Enterprise only, human review. Nothing about queries, dashboards or alerts in the destination. Uses Gemini (gemini-3.7-flash). [docs](https://docs.bindplane.com/feature-guides/pipeline-intelligence/global-pipeline-intelligence), [acquisition](https://www.dynatrace.com/news/blog/dynatrace-to-acquire-bindplane-telemetry-pipeline/) | No — content-based, not usage-based |

The raw evidence for usage exists in Grail: DQL executions from dashboards, notebooks, workflows and
API are recorded in `dt.system.query_executions` ([community thread](https://community.dynatrace.com/t5/DQL/How-to-get-data-about-queries-from-old-classic-dynatrace-screens/td-p/293320);
no official schema page found — `TODO(verify)`). Classic dashboards do not run DQL, so their usage is
not in that table.

**Biggest risk:** Dynatrace now owns both halves (Grail usage + Bindplane recommendations). Joining
them is an obvious roadmap item. Pitch Sift as "what this looks like when you join them", not as a
category nobody has thought of.

## 2. Competitors — the category exists elsewhere

| Vendor | Usage-based reduction | Signals | Notes |
|---|---|---|---|
| Grafana Cloud — Adaptive Telemetry | Yes, the reference product | metrics, logs, traces, profiles (profiles GA Aug 2026) | Adaptive Logs checks which patterns were queried in the last 15 days and recommends drop rates; Adaptive Metrics uses dashboards, alerts, rules, queries; Auto-Apply GA Oct 2026. Grafana Cloud only. [press](https://grafana.com/press/2026/08/04/grafana-labs-gives-every-observability-signal-an-intelligent-optimization-layer-completing-adaptive-telemetry-suite-with-adaptive-profiles-ga/), [Adaptive Logs docs](https://grafana.com/docs/grafana-cloud/adaptive-telemetry/adaptive-logs/manage-recommendations/), [ObsCON 2026](https://grafana.com/press/2026/10/20/observabilitycon-2026-cloud-economics/) |
| Chronosphere | Yes | logs (utility score per pattern), metrics | Logs Usage shows volume and how often each pattern is queried; Control Rules turn it into recommendations. [docs](https://docs.chronosphere.io/investigate/analyze/logs-usage), [blog](https://chronosphere.io/learn/usage-analysis-optimize-log-data/) |
| Splunk Observability Cloud | Yes, metrics | metrics | Usage Analytics + Metrics Rule Recommendations; automated archiving of metrics not used in charts/detectors/API for 30–60 days. [docs](https://help.splunk.com/en/splunk-observability-cloud/monitor-infrastructure/metrics-pipeline-management/archive-your-metrics-to-reduce-billing-cost) |
| Datadog | Partial | metrics (unqueried 30/60/90 d); logs in preview | Metrics without Limits lists unqueried metrics. Log Optimizer (Preview) finds repetitive patterns and checks them against monitors. [MwL](https://docs.datadoghq.com/metrics/metrics-without-limits/), [Log Optimizer](https://docs.datadoghq.com/logs/log_configuration/log_optimizer/) |
| New Relic | No (found) | — | Pipeline Control cloud rules = mechanism only. [docs](https://docs.newrelic.com/docs/new-relic-control/pipeline-control/cloud-rules/create-pipeline-rules/) |
| Cribl, Edge Delta, Sawmills, Bindplane | Pipeline vendors | all | Reduce by content (empty fields, dedupe, sampling). They do not see downstream query usage. [Edge Delta + Dynatrace](https://edgedelta.com/company/blog/optimize-dynatrace-observability-with-edge-delta), [Sawmills](https://venturebeat.com/business/sawmills-emerges-from-stealth-to-trim-enterprise-observability-costs-and-provide-telemetry-data-sovereignty) |

**Positioning:** "Adaptive Telemetry for Dynatrace": usage evidence from Grail + rules for
OpenPipeline or the OTel Collector/Bindplane. Differentiators worth defending:
1. **Usage-based, not content-based** (vs. Bindplane, Cribl, Edge Delta).
2. **Verify and automatic revert** after apply, with blast radius (vs. Grafana's recommendations / auto-apply).
3. **Honest money**: says where the saving actually comes from (see 4).

## 3. Is it Dynatrace-only?

- Dynatrace-specific: reading volume and usage (Grail DQL, `dt.system.query_executions`, documents,
  billing usage events), the OpenPipeline apply target, Davis problems in verify.
- Vendor-neutral: OTel Collector rules, the agent, guardrails, the proposal lifecycle, the UI, the MCP server.
- Another backend (e.g. OTel Collector + Prometheus/Loki/Grafana) needs new adapters for volume
  (Prometheus TSDB stats, Loki ingest stats), usage (Prometheus query log, Grafana dashboard and
  alert-rule APIs) and verify. Grafana Cloud users already get Adaptive Telemetry, so the payoff is
  self-hosted LGTM only. **Not worth it for the hackathon**; say "pluggable adapters" in the pitch.

## 4. Finding that changes the spec: where the drop happens decides the saving

Dynatrace DPS docs: "filtering generates usage for Ingest & Process, but not for Retain"
([Log Analytics capability](https://docs.dynatrace.com/docs/license/capabilities/log-analytics)).
Ingest is "the amount of raw data … sent to Dynatrace" ([Ingest & Process](https://docs.dynatrace.com/docs/license/capabilities/log-analytics/dps-log-ingest));
Retain is billed per GiB-day ([Retain](https://docs.dynatrace.com/docs/license/capabilities/log-analytics/dps-log-retain)).

So an **OpenPipeline drop saves Retain (and Query) but not Ingest & Process**. A drop in the OTel
Collector/Bindplane before Dynatrace should also save Ingest (inferred from "sent to Dynatrace",
not stated — `TODO(verify)`).

Today `fixtures/settings.json` uses one all-in €/GiB rate per data type, so OpenPipeline proposals
overstate savings. Proposed change (needs the user's OK, then the `sift-web-app` session):
- Split the cost model into ingest €/GiB and retain €/GiB-day (+ retention days) per data type.
- Compute savings per apply target; show both on a proposal ("€X in OpenPipeline, €Y at the Collector").
- Update `docs/DEMO_WORLD.md` figures accordingly.

## 5. How to try it

**Now, offline:** `make dev` → http://localhost:5173. Open a proposal awaiting approval → Evidence,
Rule, Impact tabs → Apply rule → watch applied → verifying → verified (8 s). Settings > Verification >
"Fail the next verification", apply another → automatic revert in Activity. Signals: check that
`recommendation` ERROR logs (case 7) and `checkout` INFO (case 8) have no proposal. Run analysis,
Ask Sift (⌘K). `make demo-reset` to start over.

**Dynatrace trial:** 15 days ([docs](https://docs.dynatrace.com/docs/get-started/get-started-with-dynatrace-saas));
via Azure Marketplace 30 days ([Microsoft](https://learn.microsoft.com/en-us/azure/partner-solutions/dynatrace/dynatrace-free-trial)).
Start it only when phase 02 code is ready. Before that, the free Playground
([signup](https://www.dynatrace.com/signup/playground/)) can be used to try DQL by hand.
Token scopes in `docs/PLAN.md` §5B are not verified yet. Telemetry: our `noisegen` (to be built)
or the OpenTelemetry Demo; both need Docker, which is not installed.

**Without Dynatrace:** `noisegen` → local OTel Collector proves only the Collector rules (volume
before/after); usage evidence needs a backend. Low value for the hackathon.

## 6. Real-user validation

Show it to: Dynatrace PMs/engineers for OpenPipeline, Log Management, cost intelligence and the
Bindplane team (Dynatrace was founded in Linz); platform/SRE engineers and FinOps people from
companies running Dynatrace.

Questions:
1. What did your last observability bill surprise you with, and who had to explain it?
2. How do you decide today which logs/metrics to cut? Who signs off, how long does it take?
3. When did someone last drop data and break a dashboard or alert? What happened?
4. Would you trust a rule that applies after one click? What evidence do you need to see?
5. Where would you apply it: OpenPipeline, Collector/Bindplane, or a ticket for the service team?
6. Would you pay for this as a share of savings, per tenant, or expect it inside Dynatrace?
7. Would Grafana-style auto-apply be acceptable for metrics? For logs?
