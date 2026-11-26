# Demo World — "noisy-shop"

This document is the **single source of truth** for fixtures, the telemetry generator (`noisegen`), offline replay datasets, and the live demo script.

## 1. System Overview

`noisy-shop` is a microservice-based e-commerce application consisting of 7 services producing metrics, logs, and distributed traces sent via OpenTelemetry Collector into Dynatrace Grail.

```
                      ┌───────────────┐
                      │   frontend    │
                      └───────┬───────┘
          ┌───────────────────┼───────────────────┐
          ▼                   ▼                   ▼
    ┌───────────┐       ┌───────────┐       ┌───────────┐
    │   cart    │       │ checkout  │       │recommend- │
    └─────┬─────┘       └─────┬─────┘       │   ation   │
          │                   │             └───────────┘
          ▼                   ▼
    ┌───────────┐       ┌───────────┐
    │ inventory │       │  payment  │
    └───────────┘       └─────┬─────┘
                              │
                              ▼
                        ┌───────────┐
                        │   email   │
                        └───────────┘
```

### Services & Roles
1. **`frontend`**: Next.js edge gateway handling web traffic, user sessions, and routing.
2. **`cart`**: In-memory shopping cart service managing item reservations.
3. **`checkout`**: Order orchestrator coordinating checkout flows, inventory checks, and billing.
4. **`payment`**: Payment processing bridge connecting to external payment gateways.
5. **`inventory`**: Warehouse stock tracking and reservation system.
6. **`email`**: Asynchronous transactional email and order confirmation sender.
7. **`recommendation`**: Machine-learning powered product recommendation engine.

---

## 2. Planted Waste Cases & Expected Outcomes

The table below defines the 8 planted cases across the 7 services, their volume share, the root cause, and the exact proposal and action Sift expects to generate.

| # | Service | Telemetry Pattern / Planted Waste | % Share | Usage Context | Expected Sift Finding & Action | Rule Type | Guardrail / Protection |
|---|---|---|---|---|---|---|---|
| **1** | `payment` | Over-verbose `DEBUG` log output (`"Processing gateway payload...", "Token exchange raw..."`) emitted on every transaction. | 35% of log volume (80.5 GiB/wk) | 0 queries in 30 days, 0 dashboards, 0 alerts. | **Finding:** `unused` / `over_verbose_level`<br>**Action:** `drop` DEBUG logs; keep INFO, WARN, ERROR.<br>**Est. Savings:** €828 / mo (345 GiB / mo). | OpenPipeline processor / OTel filter | None (safe to drop) |
| **2** | `frontend` | High-frequency `GET /healthz` HTTP health check spans emitted every 250ms by Kubernetes probes. | 25% of span volume (33.75 GiB/wk) | 0 queries, 0 dashboards, 0 alerts. | **Finding:** `noise` (health check)<br>**Action:** `drop` spans where `http.route == "/healthz"` and `http.response.status_code == 200` (failed probes stay traced).<br>**Est. Savings:** €304 / mo (145 GiB / mo). | OTel Collector `filter` processor / OpenPipeline | None (safe to drop) |
| **3** | `cart` | Duplicate stack traces: on network timeout, cart catches and re-logs the identical 45-line stack trace 3× in nested try-catch blocks. | 12% of log volume (27.6 GiB/wk, all three copies) | Queried only during incident triage; 2 queries in 30d. | **Finding:** `duplicate` stack trace<br>**Action:** `drop` the two re-logged copies (matched by `code.function.name`), keep the original from `InventoryClient`. OTel alternative: `log_dedup` processor.<br>**Est. Savings:** €189 / mo (2/3 of the pattern, 79 GiB / mo). | OpenPipeline DQL / OTel transform | None (safe to dedupe) |
| **4** | `inventory` | Periodic `"heartbeat ok - worker alive"` log line emitted every 1s per worker thread (10 workers). | 8% of log volume (18.4 GiB/wk) | 0 queries, 0 dashboards. | **Finding:** `noise` (heartbeat)<br>**Action:** `logs_to_metric` (aggregate to `inventory.heartbeat.count` metric, drop raw log lines).<br>**Est. Savings:** €189 / mo after the cost of the new metric. | OpenPipeline metric extraction / OTel `count` connector + `filter` | None (safe to aggregate) |
| **5** | `frontend` / `checkout` | High cardinality metric dimension: `user.id` (UUID per visitor, 100k+ distinct values) attached to metric `http.server.request.duration`. | 60% of metric volume (21 GiB/wk) | Dashboards query metric aggregated by `service.name` and `http.route`, never by `user.id`. | **Finding:** `high_cardinality`<br>**Action:** `drop_field` (remove `user.id` dimension from metric stream).<br>**Est. Savings:** €180 / mo (90 GiB / mo). | OpenPipeline metric processor / OTel `transform` | None (safe to drop dimension) |
| **6** | `email` | Sensitive PII leak: transactional email logs output unmasked customer email addresses (`"Sent confirmation to user: alice@example.com"`). | 4% of log volume (9.2 GiB/wk) | Queried by support team (5 queries in 30d). | **Finding:** `pii_risk`<br>**Action:** `redact` (mask email addresses to `***@***.com`), **NOT drop** because the log is used for delivery auditing.<br>**Est. Savings:** Compliance / Risk reduction. | OpenPipeline DQL `replacePattern` / OTel `transform` | **Guardrail:** Never drop active audit/support log; redact instead. |
| **7** | `recommendation` | Noisy `ERROR` logs: unhandled `ModelWeightsExpiredException` thrown continuously in background worker. | 6% of log volume (13.8 GiB/wk) | 0 queries in 30d, but level is ERROR. | **Finding:** `noise` / high volume error.<br>**Action:** **Guardrail triggered: NO DROP PROPOSAL.** Agent explains why ERROR logs cannot be dropped, calculates potential savings, and advises engineering to fix the underlying model cache bug. | N/A (Advisory only) | **Hard Guardrail:** Never drop or sample ERROR/FATAL logs. |
| **8** | `checkout` | High volume `INFO` logs (`"Order placed: order_id=...", "Payment confirmed"`) . | 18% of log volume (41.4 GiB/wk) | Actively queried by "Executive Revenue Dashboard" (180 queries/30d) and "Order Flow Failure Alert". | **Finding:** `used_signal`<br>**Action:** **Protected: NO PROPOSAL.** Shows Sift usage awareness and blast radius protection. | N/A (Protected) | **Hard Guardrail:** Never propose dropping data referenced by active alerts, SLOs, or high-frequency dashboards in the last 90 days. |

---

## 3. Telemetry Ingest & Volume Breakdown

Total baseline telemetry ingest for `noisy-shop` across 7 days: **412 GiB** (~€3,940/month estimated cost).

### Signal Composition
- **Logs:** 230 GiB (55.8%)
  - Payment DEBUG: 80.5 GiB (35.0% of logs)
  - Checkout INFO (Protected): 41.4 GiB (18.0% of logs)
  - Cart Duplicates: 27.6 GiB (12.0% of logs)
  - Inventory Heartbeats: 18.4 GiB (8.0% of logs)
  - Recommendation Errors: 13.8 GiB (6.0% of logs)
  - Email PII: 9.2 GiB (4.0% of logs)
  - Other normal service logs: 39.1 GiB (17.0% of logs)
- **Spans / Traces:** 135 GiB (32.8%)
  - Frontend `/healthz`: 33.75 GiB (25.0% of spans)
  - Frontend & Checkout standard traces: 101.25 GiB (75.0% of spans)
- **Metrics:** 35 GiB (8.5%)
  - High-cardinality `user.id` dimension series: 21 GiB (60.0% of metrics)
  - Standard service metrics: 14 GiB (40.0% of metrics)
- **Events:** 12 GiB (2.9%)

### Target Optimization Outcome
All € figures use the demo's own rates (Settings > Cost model; all-in € per GiB, not a Dynatrace
price list): logs €2.40, spans €2.10, metrics €2.00, events €1.20. A month is 30 days.

- **Baseline cost:** 412 GiB/week ≈ 1,766 GiB/month ≈ **€3,942 / month**.
- **Actionable waste (cases 1–5):** 172 GiB/week (42% of ingest), **€1,690 / month**.
  Case 6 is a redaction (no saving, removes personal data). Cases 7 and 8 produce no proposal.

These numbers are computed by `server/server/engine.py` from what the discovery pipeline
derives from the recordings in `fixtures/dynatrace/` (synthesised from `fixtures/demo_world.json`
by `make fixtures-synth`); this table documents them, the code does not read it.

## 4. What the demo starts with

`make demo-reset` replays `fixtures/scenario.json` through the same code the API uses, relative
to the moment of the reset:

| When | What happened |
|---|---|
| 6 days ago | Proposal "Remove db.statement from inventory SELECT spans" approved, **failed verification** (a dashboard tile used the attribute through a variable the usage parser missed) and was **reverted automatically**. |
| 5 days ago | Proposal "Drop static asset spans from frontend" **rejected** ("The web team uses static asset spans to debug the CDN."), then a custom guardrail was added for them. |
| 3 days ago | Case 4 (heartbeat → metric) approved and **verified**. |
| 2 days ago | Case 5 (`user.id` dimension) approved and **verified**. |
| yesterday | Log rate changed from €2.20 to €2.40 per GiB. |
| 14 min ago | Scheduled analysis created cases 1, 2, 3 and 6: **4 proposals await approval** (€1,321 / month). |

So the Overview opens with ingest of about 385 GiB in the last 7 days (lower than the 412 GiB/week
baseline because of week-over-week growth and the two verified rules), est. cost €3,573 / month,
and €369 / month already saved.
