"""Dynatrace connectors behind small interfaces, each with a Live and a Fixture
implementation (and a Recording wrapper for `make fixtures`)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from server.connectors.base import ConnectorError, DynatraceEnv
from server.connectors.documents import (
    DocumentsClient,
    FixtureDocumentsClient,
    LiveDocumentsClient,
)
from server.connectors.dtconfig import ConfigClient, FixtureConfigClient, LiveConfigClient
from server.connectors.grail import CostGuard, FixtureGrailClient, GrailClient, LiveGrailClient
from server.connectors.mcp_client import DynatraceMcp, FixtureDynatraceMcp, LiveDynatraceMcp
from server.connectors.store import FixtureStore

__all__ = ["ConnectorError", "Connectors", "DynatraceEnv", "fixture_connectors", "live_connectors"]


@dataclass
class Connectors:
    grail: GrailClient
    documents: DocumentsClient
    config: ConfigClient
    mcp: DynatraceMcp
    mode: str


def fixture_connectors(root: Path, now: datetime, guard: CostGuard | None = None) -> Connectors:
    store = FixtureStore(root, now)
    return Connectors(
        grail=FixtureGrailClient(store, guard),
        documents=FixtureDocumentsClient(store),
        config=FixtureConfigClient(store),
        mcp=FixtureDynatraceMcp(store),
        mode="fixture",
    )


def live_connectors(env: DynatraceEnv, guard: CostGuard | None = None) -> Connectors:
    return Connectors(
        grail=LiveGrailClient(env, guard),
        documents=LiveDocumentsClient(env),
        config=LiveConfigClient(env),
        mcp=LiveDynatraceMcp(env),
        mode="live",
    )
