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
| **1** | `payment` | Over-verbose `DEBUG` log output (`"Processing gateway payload...", "Token exchange raw..."`) emitted on every transaction. | ~35% of log volume (~145 GiB/wk) | 0 queries in 30 days, 0 dashboards, 0 alerts. | **Finding:** `unused` / `over_verbose_level`<br>**Action:** `drop` DEBUG logs; keep INFO, WARN, ERROR.<br>**Est. Savings:** ~€420 / mo. | OpenPipeline processor / OTel filter | None (safe to drop) |
| **2** | `frontend` | High-frequency `GET /healthz` HTTP health check spans emitted every 250ms by Kubernetes probes. | ~25% of span volume (~110 GiB/wk) | 0 queries, 0 dashboards, 0 alerts. | **Finding:** `noise` (health check)<br>**Action:** `drop` spans where `http.target == "/healthz"` and `http.status_code == 200`.<br>**Est. Savings:** ~€310 / mo. | OTel Collector `filter` processor / OpenPipeline | None (safe to drop) |
| **3** | `cart` | Duplicate stack traces: on network timeout, cart catches and re-logs the identical 45-line stack trace 3× in nested try-catch blocks. | ~12% of log volume (~50 GiB/wk) | Queried only during incident triage; 2 queries in 30d. | **Finding:** `duplicate` stack trace<br>**Action:** `dedupe` / keep first instance within 500ms window, or drop repeated nested frames.<br>**Est. Savings:** ~€140 / mo. | OpenPipeline DQL / OTel transform | None (safe to dedupe) |
| **4** | `inventory` | Periodic `"heartbeat ok - worker alive"` log line emitted every 1s per worker thread (10 workers). | ~8% of log volume (~33 GiB/wk) | 0 queries, 0 dashboards. | **Finding:** `noise` (heartbeat)<br>**Action:** `logs_to_metric` (aggregate to `inventory.heartbeat.count` metric, drop raw log lines).<br>**Est. Savings:** ~€95 / mo. | OpenPipeline metric extraction / OTel transform | None (safe to aggregate) |
| **5** | `frontend` / `checkout` | High cardinality metric dimension: `user.id` (UUID per visitor, 100k+ distinct values) attached to metric `http.server.request.duration`. | ~60% of metric cardinality | Dashboards query metric aggregated by `service.name` and `http.route`, never by `user.id`. | **Finding:** `high_cardinality`<br>**Action:** `drop_field` (remove `user.id` dimension from metric stream).<br>**Est. Savings:** ~€80 / mo. | OpenPipeline metric processor / OTel `transform` | None (safe to drop dimension) |
| **6** | `email` | Sensitive PII leak: transactional email logs output unmasked customer email addresses (`"Sent confirmation to user: alice@example.com"`). | ~4% of log volume (~16 GiB/wk) | Queried by support team (5 queries in 30d). | **Finding:** `pii_risk`<br>**Action:** `redact` (mask email addresses to `***@***.com`), **NOT drop** because the log is used for delivery auditing.<br>**Est. Savings:** Compliance / Risk reduction. | OpenPipeline DQL `replace_pattern` / OTel `transform` | **Guardrail:** Never drop active audit/support log; redact instead. |
| **7** | `recommendation` | Noisy `ERROR` logs: unhandled `ModelWeightsExpiredException` thrown continuously in background worker. | ~6% of log volume (~25 GiB/wk) | 0 queries in 30d, but level is ERROR. | **Finding:** `noise` / high volume error.<br>**Action:** **Guardrail triggered: NO DROP PROPOSAL.** Agent explains why ERROR logs cannot be dropped, calculates potential savings, and advises engineering to fix the underlying model cache bug. | N/A (Advisory only) | **Hard Guardrail:** Never drop or sample ERROR/FATAL logs. |
| **8** | `checkout` | High volume `INFO` logs (`"Order placed: order_id=...", "Payment confirmed"`) (~18% of log volume). | ~18% of log volume (~75 GiB/wk) | Actively queried by "Executive Revenue Dashboard" (180 queries/30d) and "Order Flow Failure Alert". | **Finding:** `used_signal`<br>**Action:** **Protected: NO PROPOSAL.** Shows Sift usage awareness and blast radius protection. | N/A (Protected) | **Hard Guardrail:** Never propose dropping data referenced by active alerts, SLOs, or high-frequency dashboards in the last 90 days. |

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
- **Total Identifiable Waste:** ~157 GiB / week (~38% of total volume).
- **Proposals Generated:** 5 actionable proposals (#1, #2, #3, #4, #5) + 1 redact proposal (#6) + 2 protected/advisory explanations (#7, #8).
- **Total Potential Monthly Savings:** **€1,120 / month** in telemetry storage and DQL scanning costs.
