"""Shared pieces of the Dynatrace connectors: errors, the HTTP client and settings."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx


class ConnectorError(RuntimeError):
    """A Dynatrace call failed in a way the caller should show, not retry."""


class FixtureMissingError(ConnectorError):
    pass


class FixtureMismatchError(ConnectorError):
    """The recorded query text differs from the query the code asks for now."""


@dataclass(frozen=True)
class DynatraceEnv:
    platform_url: str  # https://<env-id>.apps.dynatrace.com
    token: str  # platform token (Bearer)
    mcp_url: str = ""
    timeout_s: float = 30.0

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}


RETRY_STATUS = (429, 502, 503, 504)


def request(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    attempts: int = 3,
    backoff_s: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs: Any,
) -> httpx.Response:
    """One HTTP call with bounded retries on 429/5xx (honours Retry-After)."""
    last: httpx.Response | None = None
    for attempt in range(attempts):
        resp = client.request(method, url, **kwargs)
        if resp.status_code not in RETRY_STATUS:
            if resp.status_code >= 400:
                raise ConnectorError(
                    f"{method} {url} -> HTTP {resp.status_code}: {resp.text[:300]}"
                )
            return resp
        last = resp
        if attempt < attempts - 1:
            retry_after = resp.headers.get("Retry-After", "")
            delay = float(retry_after) if retry_after.isdigit() else backoff_s * 2**attempt
            sleep(min(delay, 20.0))
    assert last is not None
    raise ConnectorError(f"{method} {url} -> HTTP {last.status_code} after {attempts} attempts")
