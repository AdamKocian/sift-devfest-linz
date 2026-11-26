"""Read-only configuration that runs DQL on a schedule: workflows, custom alerts, SLOs.

- Workflows: GET /platform/automation/v1/workflows (limit/offset)  scope automation:workflows:read
  https://docs.dynatrace.com/docs/dynatrace-api/platform-api/platform-automation-v1/workflows-get
  -> {"count": n, "results": [{"id", "title", "owner", "trigger": {"schedule": {...}},
      "tasks": {<name>: {"name", "action", "input": {...}}}}]}
- SLOs: GET /platform/slo/v1/slos (page-size<=500, page-key)  scope slo:slos:read
  https://docs.dynatrace.com/docs/dynatrace-api/platform-api/platform-slo-v1/slos-get
  -> {"totalCount", "nextPageKey", "slos": [...]}; exactly one of sliReference / customSli.
- Custom alerts (DQL anomaly detectors) live in Settings since Dynatrace 1.344:
  https://docs.dynatrace.com/docs/dynatrace-intelligence/anomaly-detection/anomaly-detection-app
  Their settings schema id and the JSON path of the query are not documented there.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from server.connectors.base import ConnectorError, DynatraceEnv, request
from server.connectors.store import FixtureStore

WORKFLOWS = "/platform/automation/v1/workflows"
SLOS = "/platform/slo/v1/slos"
# TODO(verify): settings endpoint on the platform URL, schema id and value fields of custom
# alerts. https://docs.dynatrace.com/docs/dynatrace-api/environment-api/settings
SETTINGS_OBJECTS = "/platform/classic/environment-api/v2/settings/objects"
ALERT_SCHEMA = "builtin:davis.anomaly-detectors"


@dataclass(frozen=True)
class QuerySite:
    """Something that runs DQL on its own: a workflow task, an alert or an SLO."""

    type: str  # workflow | alert | slo
    id: str
    name: str
    owner: str
    dql: str
    every_minutes: float | None = None  # how often it runs, when the config says so
    part: str | None = None  # workflow task name


class ConfigClient(Protocol):
    def workflows(self) -> list[QuerySite]: ...

    def alerts(self) -> list[QuerySite]: ...

    def slos(self) -> list[QuerySite]: ...


_INTERVAL_MIN = {"m": 1, "h": 60, "d": 1440}


def _every(schedule: dict[str, Any] | None) -> float | None:
    """Workflow schedule trigger -> minutes between runs (interval triggers only)."""
    trig = (schedule or {}).get("trigger") or {}
    if trig.get("type") == "interval" and trig.get("intervalMinutes"):
        return float(trig["intervalMinutes"])
    return None


def parse_workflows(body: dict[str, Any]) -> list[QuerySite]:
    """Every task whose input carries a DQL `query` is a query site.

    TODO(verify): the DQL task action id (dynatrace.automations:execute-dql-query) and its
    input field are not on the API page; matching on `input.query` works for any action."""
    out = []
    for wf in body.get("results", []):
        schedule = (wf.get("trigger") or {}).get("schedule")
        for key, task in (wf.get("tasks") or {}).items():
            query = (task.get("input") or {}).get("query")
            if isinstance(query, str) and query.strip():
                out.append(
                    QuerySite(
                        type="workflow",
                        id=wf["id"],
                        name=wf.get("title") or wf["id"],
                        owner=wf.get("owner", ""),
                        dql=query,
                        every_minutes=_every(schedule),
                        part=task.get("name") or key,
                    )
                )
    return out


def parse_slos(body: dict[str, Any]) -> list[QuerySite]:
    """TODO(verify): customSli.indicator holds the DQL; the CustomSli body is not rendered on
    https://docs.dynatrace.com/docs/dynatrace-api/platform-api/platform-slo-v1/slos-get"""
    out = []
    for slo in body.get("slos", []):
        indicator = (slo.get("customSli") or {}).get("indicator")
        if isinstance(indicator, str) and indicator.strip():
            out.append(
                QuerySite(
                    type="slo",
                    id=slo["id"],
                    name=slo.get("name") or slo["id"],
                    owner=slo.get("owner", ""),
                    dql=indicator,
                    every_minutes=float(slo.get("evaluationIntervalMinutes") or 0) or None,
                )
            )
    return out


def parse_alerts(body: dict[str, Any]) -> list[QuerySite]:
    """TODO(verify): value.title / value.enabled / value.source.query / value.source.interval
    are assumed field names of the custom-alert settings value."""
    out = []
    for item in body.get("items", []):
        value = item.get("value") or {}
        source = value.get("source") or {}
        query = source.get("query")
        if not value.get("enabled", True) or not isinstance(query, str):
            continue
        every = None
        interval = str(source.get("interval") or "")
        if interval[:-1].isdigit() and interval[-1:] in _INTERVAL_MIN:
            every = float(int(interval[:-1]) * _INTERVAL_MIN[interval[-1]])
        out.append(
            QuerySite(
                type="alert",
                id=item["objectId"],
                name=value.get("title") or item["objectId"],
                owner=item.get("owner", "") or value.get("owner", ""),
                dql=query,
                every_minutes=every,
            )
        )
    return out


class LiveConfigClient:
    def __init__(self, env: DynatraceEnv, client: httpx.Client | None = None) -> None:
        self.http = client or httpx.Client(
            base_url=env.platform_url, headers=env.headers(), timeout=env.timeout_s
        )
        self.raw: dict[str, Any] = {}

    def workflows(self) -> list[QuerySite]:
        results: list[dict[str, Any]] = []
        offset = 0
        while True:
            body = request(
                self.http, "GET", WORKFLOWS, params={"limit": 100, "offset": offset}
            ).json()
            page = body.get("results", [])
            results.extend(page)
            offset += len(page)
            if not page or offset >= int(body.get("count", 0)):
                break
        self.raw["workflows"] = {"count": len(results), "results": results}
        return parse_workflows(self.raw["workflows"])

    def slos(self) -> list[QuerySite]:
        slos: list[dict[str, Any]] = []
        key: str | None = None
        for _ in range(100):
            params: dict[str, Any] = {"page-key": key} if key else {"page-size": 500}
            body = request(self.http, "GET", SLOS, params=params).json()
            slos.extend(body.get("slos", []))
            key = body.get("nextPageKey")
            if not key:
                self.raw["slos"] = {"totalCount": len(slos), "slos": slos}
                return parse_slos(self.raw["slos"])
        raise ConnectorError("SLO list did not end after 100 pages")

    def alerts(self) -> list[QuerySite]:
        items: list[dict[str, Any]] = []
        key: str | None = None
        for _ in range(100):
            params: dict[str, Any] = (
                {"nextPageKey": key}
                if key
                else {"schemaIds": ALERT_SCHEMA, "pageSize": 500, "fields": "objectId,value"}
            )
            body = request(self.http, "GET", SETTINGS_OBJECTS, params=params).json()
            items.extend(body.get("items", []))
            key = body.get("nextPageKey")
            if not key:
                self.raw["alerts"] = {"items": items}
                return parse_alerts(self.raw["alerts"])
        raise ConnectorError("settings list did not end after 100 pages")


class FixtureConfigClient:
    def __init__(self, store: FixtureStore) -> None:
        self.store = store

    def workflows(self) -> list[QuerySite]:
        return parse_workflows(self.store.read("automation", "workflows.json")["response"])

    def slos(self) -> list[QuerySite]:
        return parse_slos(self.store.read("slo", "slos.json")["response"])

    def alerts(self) -> list[QuerySite]:
        return parse_alerts(self.store.read("settings", "custom-alerts.json")["response"])
