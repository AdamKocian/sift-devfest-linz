"""Make case 8 "used" on a real tenant: create the dashboards and notebooks of the demo world
and run their queries once, so query executions exist. Custom alerts must be created in
the UI (their settings schema is not documented, see TODO below).

APIs:
- Create document: POST /platform/document/v1/documents, multipart fields name, type,
  content. TODO(verify): field names; the create page was not read.
  https://docs.dynatrace.com/docs/dynatrace-api/platform-api/platform-document-v1
- Run DQL: POST /platform/storage/query/v1/query:execute (+ query:poll), documented in
  https://docs.dynatrace.com/docs/dynatrace-api/platform-api/platform-storage-query-v1
Token scopes: document:documents:write, storage:logs:read, storage:spans:read,
storage:metrics:read (TODO(verify): document:documents:write is named in the
dynatrace-oss/dynatrace-mcp README, not on the API page).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

QUERY = "/platform/storage/query/v1"
DOCS = "/platform/document/v1/documents"


def _documents(fixtures: Path) -> list[dict[str, Any]]:
    out = []
    for f in sorted((fixtures / "dynatrace" / "documents").glob("*.json")):
        if f.name != "list.json":
            out.append(json.loads(f.read_text()))
    return out


def _queries(content: dict[str, Any]) -> list[str]:
    tiles = [t["query"] for t in (content.get("tiles") or {}).values() if t.get("query")]
    sections = [
        s["state"]["input"]["value"]
        for s in content.get("sections") or []
        if s.get("type") == "dql"
    ]
    return [q for q in tiles + sections if "$" not in q]


def _run(http: httpx.Client, dql: str) -> str:
    body = {"query": dql, "requestTimeoutMilliseconds": 10_000, "defaultScanLimitGbytes": 1}
    r = http.post(f"{QUERY}/query:execute", json=body)
    r.raise_for_status()
    data = r.json()
    for _ in range(30):
        if data.get("state") not in ("RUNNING", "NOT_STARTED"):
            break
        time.sleep(1)
        r = http.get(f"{QUERY}/query:poll", params={"request-token": data["requestToken"]})
        r.raise_for_status()
        data = r.json()
    return str(data.get("state"))


def setup(fixtures: str, dry_run: bool = False) -> int:
    docs = _documents(Path(fixtures))
    if dry_run:
        for d in docs:
            m = d["metadata"]
            n = len(_queries(d["content"]))
            print(f"would create {m['type']} {m['name']!r} and run {n} queries")
        return 0
    url, token = os.getenv("DT_PLATFORM_URL", ""), os.getenv("DT_PLATFORM_TOKEN", "")
    if not url or not token:
        print("set DT_PLATFORM_URL and DT_PLATFORM_TOKEN", file=sys.stderr)
        return 2
    http = httpx.Client(base_url=url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    for d in docs:
        m = d["metadata"]
        files = {
            "name": (None, m["name"]),
            "type": (None, m["type"]),
            "content": ("content.json", json.dumps(d["content"]), "application/json"),
        }
        r = http.post(DOCS, files=files)
        print(f"create {m['type']} {m['name']!r}: HTTP {r.status_code}")
        for q in _queries(d["content"]):
            print(f"  run query: {_run(http, q)}")
    print(
        "\nCreate the custom alert 'Order Flow Failure' in Settings > Analyze and alert > "
        "Custom alerts with this query (TODO(verify): no documented API schema):\n"
        '  fetch logs | filter service.name == "checkout" and matchesPhrase(content, '
        '"Payment confirmed") | makeTimeseries confirmed = count(), interval: 1m'
    )
    return 0
