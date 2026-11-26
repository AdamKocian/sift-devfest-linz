"""`make fixtures`: run discovery against a live tenant and record every response into
fixtures/dynatrace/, redacted, where the fixture connectors read them.

Redaction: the environment id becomes `abc12345`, tokens are removed, e-mail addresses
are replaced by user<n>@example.com (stable per address), and log content is masked the
same way the patterns engine masks examples. Review the diff before committing: the
repository is public."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from server.connectors import Connectors, DynatraceEnv
from server.connectors.documents import LiveDocumentsClient
from server.connectors.dtconfig import LiveConfigClient
from server.connectors.grail import CostGuard, LiveGrailClient, RecordingGrailClient
from server.connectors.mcp_client import LiveDynatraceMcp
from server.connectors.store import FixtureStore, format_ts, safe_name
from server.engines import patterns, pipeline
from server.engines.usage import NoResolver

FAKE_ENV = "abc12345"
_TOKEN = re.compile(r"dt0[a-z]\d{2}\.[A-Za-z0-9._-]+")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


class Redactor:
    def __init__(self, env_id: str) -> None:
        self.env_id = env_id
        self.emails: dict[str, str] = {}

    def _email(self, m: re.Match[str]) -> str:
        addr = m.group(0)
        if addr.endswith("@example.com"):
            return addr
        return self.emails.setdefault(addr, f"user{len(self.emails) + 1}@example.com")

    def text(self, s: str, key: str = "") -> str:
        if self.env_id:
            s = s.replace(self.env_id, FAKE_ENV)
        s = _TOKEN.sub("<token>", s)
        s = _EMAIL.sub(self._email, s)
        if key == "content":
            s = patterns.redact(s)
        return s

    def __call__(self, obj: Any, key: str = "") -> Any:
        if isinstance(obj, dict):
            return {k: self(v, k) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self(v, key) for v in obj]
        if isinstance(obj, str):
            return self.text(obj, key)
        return obj


def env_id(url: str) -> str:
    return url.split("//", 1)[-1].split(".", 1)[0]


def record(env: DynatraceEnv, settings: dict[str, Any], out: Path) -> pipeline.Snapshot:
    redact = Redactor(env_id(env.platform_url))
    store = FixtureStore(out)
    now = datetime.now(UTC)
    live = LiveGrailClient(env, CostGuard())
    docs, cfg, mcp = LiveDocumentsClient(env), LiveConfigClient(env), LiveDynatraceMcp(env)
    conn = Connectors(
        grail=RecordingGrailClient(live, store, redact, lambda: now),
        documents=docs,
        config=cfg,
        mcp=mcp,
        mode="record",
    )
    snap = pipeline.run(conn, settings, now, NoResolver())
    at = format_ts(now)
    store.write({"recorded_at": at, "pages": redact(docs.raw_pages)}, "documents", "list.json")
    for doc_id, content in docs.raw_content.items():
        store.write(
            {"recorded_at": at, "content": redact(content)},
            "documents",
            f"{safe_name(redact.text(doc_id))}.json",
        )
    for name, folder, file in (
        ("workflows", "automation", "workflows.json"),
        ("alerts", "settings", "custom-alerts.json"),
        ("slos", "slo", "slos.json"),
    ):
        if name in cfg.raw:
            store.write({"recorded_at": at, "response": redact(cfg.raw[name])}, folder, file)
    if mcp.raw is not None:
        store.write({"recorded_at": at, "response": redact(mcp.raw)}, "mcp", "tools.json")
    store.write({"recorded_at": at, "id": FAKE_ENV, "name": FAKE_ENV}, "environment.json")
    return snap
