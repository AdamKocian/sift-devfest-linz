"""Dashboards and notebooks from the Document service.

API: https://docs.dynatrace.com/docs/dynatrace-api/platform-api/platform-document-v1
- GET /platform/document/v1/documents?filter=&page-size=(<=1000)&page-key=
  scope document:documents:read
- GET /platform/document/v1/documents/{id}  -> metadata and content in one multipart response
Content structure:
- dashboards: https://docs.dynatrace.com/docs/analyze-explore-automate/dashboards-and-notebooks/document-api/document-structure-dashboards
  {"version": 21, "variables": [...], "tiles": {"1": {"type": "data", "query": "..."}}}
- notebooks:  https://docs.dynatrace.com/docs/analyze-explore-automate/dashboards-and-notebooks/document-api/document-structure-notebooks
  {"version": "7", "sections": [{"type": "dql", "state": {"input": {"value": "..."}}}]}
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from email.parser import BytesParser
from email.policy import default as email_policy
from typing import Any, Protocol

import httpx

from server.connectors.base import ConnectorError, DynatraceEnv, request
from server.connectors.store import FixtureStore, parse_ts, safe_name

DOC_BASE = "/platform/document/v1/documents"
TYPES = ("dashboard", "notebook")


@dataclass(frozen=True)
class DocumentMeta:
    id: str
    name: str
    type: str
    owner: str
    last_modified: datetime | None


class DocumentsClient(Protocol):
    def list(self) -> list[DocumentMeta]: ...

    def content(self, doc_id: str) -> dict[str, Any]: ...


def parse_list(body: dict[str, Any]) -> tuple[list[DocumentMeta], str | None]:
    """One page of the list response.

    TODO(verify): the response schema is not on the docs page; `documents`, `nextPageKey`
    and `modificationInfo.lastModifiedTime` are the field names the filter and sort
    parameters use. https://docs.dynatrace.com/docs/dynatrace-api/platform-api/platform-document-v1/documents-get
    """
    out = []
    for d in body.get("documents", []):
        mod = (d.get("modificationInfo") or {}).get("lastModifiedTime")
        out.append(
            DocumentMeta(
                id=d["id"],
                name=d.get("name") or d["id"],
                type=d.get("type", ""),
                owner=d.get("owner", ""),
                last_modified=parse_ts(mod) if mod else None,
            )
        )
    return out, body.get("nextPageKey")


def parse_multipart(content_type: str, raw: bytes) -> dict[str, bytes]:
    """multipart/form-data body -> {part name: bytes}."""
    head = f"Content-Type: {content_type}\r\n\r\n".encode()
    msg = BytesParser(policy=email_policy).parsebytes(head + raw)
    parts: dict[str, bytes] = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        payload = part.get_payload(decode=True)
        if isinstance(name, str) and isinstance(payload, bytes):
            parts[name] = payload
    return parts


class LiveDocumentsClient:
    def __init__(self, env: DynatraceEnv, client: httpx.Client | None = None) -> None:
        self.http = client or httpx.Client(
            base_url=env.platform_url, headers=env.headers(), timeout=env.timeout_s
        )
        self.raw_pages: list[dict[str, Any]] = []
        self.raw_content: dict[str, dict[str, Any]] = {}

    def list(self) -> list[DocumentMeta]:
        out: list[DocumentMeta] = []
        key: str | None = None
        self.raw_pages = []
        for _ in range(100):
            params: dict[str, Any] = {"page-size": 1000}
            if key:
                params["page-key"] = key  # with page-key the other parameters must go
            else:
                params["filter"] = "type in ('dashboard', 'notebook')"
            body = request(self.http, "GET", DOC_BASE, params=params).json()
            self.raw_pages.append(body)
            page, key = parse_list(body)
            out.extend(page)
            if not key:
                return out
        raise ConnectorError("document list did not end after 100 pages")

    def content(self, doc_id: str) -> dict[str, Any]:
        resp = request(self.http, "GET", f"{DOC_BASE}/{doc_id}")
        ctype = resp.headers.get("content-type", "")
        if ctype.startswith("multipart/"):
            # TODO(verify): part names "metadata" and "content".
            # https://docs.dynatrace.com/docs/dynatrace-api/platform-api/platform-document-v1/documents-id-get
            parts = parse_multipart(ctype, resp.content)
            raw = parts.get("content")
            if raw is None:
                raise ConnectorError(f"document {doc_id}: no content part in {list(parts)}")
            content = json.loads(raw)
        else:
            content = resp.json()
        self.raw_content[doc_id] = content
        return content


class FixtureDocumentsClient:
    """fixtures/dynatrace/documents/list.json (list pages) + <id>.json (content)."""

    def __init__(self, store: FixtureStore) -> None:
        self.store = store

    def list(self) -> list[DocumentMeta]:
        out: list[DocumentMeta] = []
        for body in self.store.read("documents", "list.json")["pages"]:
            out.extend(parse_list(body)[0])
        return out

    def content(self, doc_id: str) -> dict[str, Any]:
        return self.store.read("documents", f"{safe_name(doc_id)}.json")["content"]
