# DQL used by Sift

Every query Sift runs is built in [`server/server/dql.py`](../server/server/dql.py), so the text
below is exactly what goes to the Grail query API (`POST /platform/storage/query/v1/query:execute`,
then `query:poll`). Fixture mode replays a recording only if its stored text matches the query
byte for byte (`fixtures/dynatrace/grail/<name>.json`).

**Status: none of these queries has run against a tenant yet. Every query is `TODO(verify)`
until the first `make discover-live`.** The "Scanned" column is an estimate: what the
query has to read, for the demo world's 412 GiB/week of ingest and for a trial tenant fed by
noisegen at its default scale (about 80 KB/min, so roughly 120 MB/day).

## Cost guard

Grail bills queries by bytes scanned. Every query goes through `CostGuard`
([`connectors/grail.py`](../server/server/connectors/grail.py)) before it is sent:

- every `fetch`, `timeseries` and `metrics` (also inside `lookup`/`join` subqueries) must have
  `from:`; the window may be at most 90 days;
- `scanLimitGBytes` may not be raised above 50 GB or set to `-1`; the request sets
  `defaultScanLimitGbytes` to 50 (the documented default is 500 GB);
- a discovery run stops once it has scanned 200 GB in total.

The fetch docs say sampled aggregates must be scaled by `dt.system.sampling_ratio`; the
volume queries do that when `discover.sampling_ratio` > 1. Fixture settings use 100 (the
demo world is large); `make discover-live` uses 1 (a trial tenant is small).

## Queries

| Name | Purpose | Reads | Scanned (demo world / trial) |
|---|---|---|---|
| `billing_ingest` | Billed ingest bytes per capability and day, to calibrate size estimates and to find data without signals (events) | `dt.system.events` | < 50 MB / < 1 MB |
| `log_volume` | Records and content length per log source per day, 15 days | logs | 4.6 GiB at samplingRatio 100 (460 GiB unsampled) / ~1.8 GB |
| `log_first_seen` | First record per log source in 90 days (sampled 1:1000) | logs | ~3 GiB / ~11 MB |
| `span_volume` | Spans per service and span name per day, plus the length of large attributes | spans | 2.7 GiB at 100 / ~1 GB |
| `span_first_seen` | First span per service and span name in 90 days (sampled 1:1000) | spans | ~1.7 GiB / ~4 MB |
| `metric_series` | Number of series per metric key and service | metric series | small (series index) |
| `metric_dimensions:<key>` | One series of a metric: its dimension keys | metric series | small |
| `metric_cardinality:<key>` | Series of a metric and how many remain without each dimension | metric series | small |
| `log_sample:<signal>` | Up to 1000 lines of one log source from the last 24 h, for Drain3 | logs | one day of that source / samplingRatio |
| `pattern_volume:<signal>` | Exact records and size of each mined pattern over 7 days (`matchesPhrase`) | logs | 7 days of that source / samplingRatio |
| `query_executions` | Who ran which DQL in 90 days, with 30-day counts | `dt.system.query_executions` | ~12 MB / < 1 MB |
| `dry_run:<signal>:<digest>` | The agent's rule matcher as a filter over the last 24 h (validate_rule) | logs or spans of one service | one day of that service / samplingRatio (cart: ~51 MiB at 100) |

### billing_ingest

```dql
fetch dt.system.events, from: -15d
| filter event.kind == "BILLING_USAGE_EVENT"
| filter in(event.type, {"Log Management & Analytics - Ingest & Process", "Traces - Ingest & Process", "Events - Ingest & Process"})
| dedup event.id
| summarize billed_bytes = sum(billed_bytes), by:{event.type, day = bin(timestamp, 1d)}
```

Sources: event kinds, types and `billed_bytes` in the
[system event model](https://docs.dynatrace.com/docs/semantic-dictionary/model/dt-system-events);
"`dedup event.id` is mandatory in every billing query" in
[Investigate a spike](https://docs.dynatrace.com/docs/manage-your-costs/control/investigate-a-spike).
Metrics have no byte-based ingest billing event in the pages we read, so metric sizes stay
uncalibrated estimates. TODO(verify).

### log_volume

```dql
fetch logs, from: -15d
| summarize records = count(), content_chars = sum(stringLength(content)),
    by:{service.name = coalesce(service.name, "unknown"), log.source = coalesce(log.source, "unknown"), loglevel = coalesce(loglevel, "NONE"), day = bin(timestamp, 1d)}
```

With `discover.sampling_ratio: 100` the fetch gets `, samplingRatio: 100` and both
aggregates are multiplied by `takeAny(dt.system.sampling_ratio)`.

DQL has no function for the stored size of a record (checked the
[functions index](https://docs.dynatrace.com/docs/platform/grail/dynatrace-query-language/functions)).
Sift estimates bytes as `content_chars + records × 180` and scales the sum to the billed
bytes of `billing_ingest`. `stringLength` counts UTF-16 code units, not bytes.
`loglevel`, `content` and `log.source` are in the
[log model](https://docs.dynatrace.com/docs/semantic-dictionary/model/log); `service.name` comes
from the OTel resource (resource attributes become top-level fields:
[ingest logs](https://docs.dynatrace.com/docs/ingest-from/opentelemetry/otlp-api/ingest-logs)).

### log_first_seen

```dql
fetch logs, from: -90d, samplingRatio: 1000
| summarize first_seen = min(timestamp), by:{service.name = coalesce(service.name, "unknown"), log.source = coalesce(log.source, "unknown"), loglevel = coalesce(loglevel, "NONE")}
```

TODO(verify): whether sampling lowers the billed scan volume.

### span_volume

```dql
fetch spans, from: -15d
| summarize records = count(),
    attr_db_statement = sum(stringLength(coalesce(db.statement, ""))),
    by:{service.name = coalesce(service.name, "unknown"), span.name, day = bin(start_time, 1d)}
```

One `attr_<name>` column per entry of `discover.span_attributes`. Spans have `start_time`,
not `timestamp` ([aggregation commands](https://docs.dynatrace.com/docs/platform/grail/dynatrace-query-language/commands/aggregation-commands)).
TODO(verify): `span.name` and `db.statement` field names (the span model page returned 404).

### span_first_seen

```dql
fetch spans, from: -90d, samplingRatio: 1000
| summarize first_seen = min(start_time), by:{service.name = coalesce(service.name, "unknown"), span.name}
```

### metric_series, metric_dimensions, metric_cardinality

```dql
metrics, from: -7d
| summarize series = count(),
    by:{metric.key, service.name = coalesce(service.name, "unknown")}
```

```dql
metrics, from: -7d
| filter metric.key == "http.server.request.duration"
| limit 1
```

```dql
metrics, from: -7d
| filter metric.key == "http.server.request.duration"
| summarize series = count(),
    without_http_request_method = countDistinctExact(concat(coalesce(toString(http.route), ""), "|", coalesce(toString(service.name), ""), "|", coalesce(toString(user.id), ""))),
    without_http_route = countDistinctExact(concat(coalesce(toString(http.request.method), ""), "|", coalesce(toString(service.name), ""), "|", coalesce(toString(user.id), ""))),
    without_service_name = countDistinctExact(concat(coalesce(toString(http.request.method), ""), "|", coalesce(toString(http.route), ""), "|", coalesce(toString(user.id), ""))),
    without_user_id = countDistinctExact(concat(coalesce(toString(http.request.method), ""), "|", coalesce(toString(http.route), ""), "|", coalesce(toString(service.name), "")))
```

Only metrics with at least `discover.dimension_min_series` (5000) series get the last two.
The `metrics` command "retrieves metric series … for exploring metric keys, dimension keys,
and values" and covers the last ten days
([metric commands](https://docs.dynatrace.com/docs/platform/grail/dynatrace-query-language/commands/metric-commands)).
`countDistinctExact` fails above 1 M distinct values
([aggregation functions](https://docs.dynatrace.com/docs/platform/grail/dynatrace-query-language/functions/aggregation-functions)).
TODO(verify): `from:` on `metrics`, dimensions as fields, and `concat`/`toString` on null.
Data points per day are estimated as series × 1440 (one point per minute).

### log_sample:&lt;signal&gt;

```dql
fetch logs, from: -24h, samplingRatio: 100
| filter service.name == "inventory" and log.source == "app" and loglevel == "INFO"
| fields content
| limit 1000
```

The sampling ratio is the largest valid value (1, 10, …, 100000) that still returns about
1000 lines for that source's daily volume.

### pattern_volume:&lt;signal&gt;

```dql
fetch logs, from: -7d
| filter service.name == "inventory" and log.source == "app" and loglevel == "INFO"
| fieldsAdd sift.pattern = if(matchesPhrase(content, "heartbeat ok"), "log-inventory-app-info-heartbeat-ok-worker-8730", else: "other")
| summarize records = count(), content_chars = sum(stringLength(content)), by:{sift.pattern}
```

Only for sources where Drain3 found a template covering at least 60 % of the sampled lines.
The phrase is the longest run of plain words in the template; `matchesPhrase` is token based
([string functions](https://docs.dynatrace.com/docs/platform/grail/dynatrace-query-language/functions/string-functions)).
The same matcher becomes the OpenPipeline rule, so this count doubles as its dry run.

### query_executions

```dql
fetch dt.system.query_executions, from: -90d
| summarize executions_30d = countIf(timestamp >= now() - 30d), last_run = max(timestamp),
    by:{query_string, client.application_context, client.source, user.email}
```

TODO(verify): there is no official schema page for `dt.system.query_executions`; it is named
in a [community thread](https://community.dynatrace.com/t5/DQL/How-to-get-data-about-queries-from-old-classic-dynatrace-screens/td-p/293320),
and the field names are assumptions. Documented alternatives, with less detail:
`QUERY_EXECUTION_EVENT` records in `dt.system.events` (`query_pool`, `client.client_context`,
`client.workflow_context`, `scanned_bytes`; no query text), and the `client.source` of
`"… - Query"` billing events (a dashboard URL, an anomaly-detector objectId, or
`dynatrace.automations:…` for workflows)
([Investigate a spike](https://docs.dynatrace.com/docs/manage-your-costs/control/investigate-a-spike)).
Sift matches an execution to a dashboard, notebook, workflow, alert or SLO when its
`client.source` contains that object's id; the rest become ad-hoc query references.

### dry_run:&lt;signal&gt;:&lt;digest&gt;

The OpenPipeline dry run of a rule the agent drafted (`validate_rule`, built by `dql.dry_run`
from the same structured matcher as the OpenPipeline processor and the OTel Collector
snippet, see [`agent/rules.py`](../server/server/agent/rules.py)). `outside_target` must be 0
(the rule stays inside its signal) and `matched_errors` must be 0 for a removing rule. The
digest identifies the matcher, so a changed rule never replays an old answer. Example (case 3):

```dql
fetch logs, from: -24h, samplingRatio: 100
| filter service.name == "cart"
| summarize scope_records = count() * takeAny(dt.system.sampling_ratio),
    target_records = countIf(<target>) * takeAny(dt.system.sampling_ratio),
    matched_records = countIf(<target> and not matchesPhrase(content, "InventoryClient.reserve")) * takeAny(dt.system.sampling_ratio),
    outside_target = countIf((<matcher>) and not (<target>)) * takeAny(dt.system.sampling_ratio),
    matched_errors = countIf((<matcher>) and in(loglevel, {"ERROR", "FATAL"})) * takeAny(dt.system.sampling_ratio)
```

with `<target>` = `service.name == "cart" and log.source == "app" and loglevel == "WARN" and
matchesPhrase(content, "Timeout calling inventory")`. A metric-dimension rule re-runs
`metric_cardinality:<key>` instead (series now and without the dimension). Fixture mode
replays the recordings in `fixtures/dynatrace/grail/dry_run_*.json`; they were answered from
the demo world by `server.synth` while the agent run was recorded (`countIf` and `in()` are in
the DQL functions reference; the query has not run on a tenant: TODO(verify)).

## Queries Sift reads but does not run

The usage engine parses the DQL of dashboards and notebooks (Document service), workflow
tasks, custom alerts and SLO indicators, see
[`engines/dql_parser.py`](../server/server/engines/dql_parser.py). Those queries cost nothing
to read.
