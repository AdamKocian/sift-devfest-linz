from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.config import Config, load_config
from server.main import create_app


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return dataclasses.replace(
        load_config(),
        db_path=tmp_path / "sift.db",
        replay_speed=0,  # no sleeps in scripted runs and chat
        background_tasks=False,  # tests drive verification explicitly
        web_dist=tmp_path / "no-dist",
    )


@pytest.fixture
def client(cfg: Config) -> Iterator[TestClient]:
    with TestClient(create_app(cfg)) as c:
        yield c
