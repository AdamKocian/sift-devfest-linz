"""Runtime configuration, read once from environment variables.

Only fixture mode exists so far. Live mode (Dynatrace + Vertex AI) arrives in phase 02.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

APPLY_TARGETS = ("dry-run", "openpipeline", "otelcol")


def _resolve(path: str) -> Path:
    """Relative paths in env vars are relative to the repository root, not the cwd."""
    p = Path(path).expanduser()
    return p if p.is_absolute() else (REPO_ROOT / p).resolve()


@dataclass(frozen=True)
class Config:
    mode: str
    apply_target: str
    db_path: Path
    fixtures_dir: Path
    verify_window_min: int
    # Fixture mode compresses waiting so a demo does not take 30 minutes.
    fixture_verify_seconds: float
    # 1.0 = recorded pacing, 0 = no sleeps (tests).
    replay_speed: float
    env_id: str
    platform_url: str
    web_dist: Path
    background_tasks: bool = True

    @property
    def is_fixture(self) -> bool:
        return self.mode == "fixture"


def load_config() -> Config:
    mode = os.getenv("SIFT_MODE", "fixture").strip() or "fixture"
    if mode not in ("fixture", "live"):
        raise ValueError(f"SIFT_MODE must be 'fixture' or 'live', got {mode!r}")
    if mode == "live":
        raise NotImplementedError(
            "SIFT_MODE=live is not implemented yet (phase 02). Use SIFT_MODE=fixture."
        )
    target = os.getenv("SIFT_APPLY_TARGET", "dry-run").strip() or "dry-run"
    if target not in APPLY_TARGETS:
        raise ValueError(f"SIFT_APPLY_TARGET must be one of {APPLY_TARGETS}, got {target!r}")
    platform_url = os.getenv("DT_PLATFORM_URL", "").strip()
    return Config(
        mode=mode,
        apply_target=target,
        db_path=_resolve(os.getenv("SIFT_DB", "./data/sift.db")),
        fixtures_dir=_resolve(os.getenv("SIFT_FIXTURES", "./fixtures")),
        verify_window_min=int(os.getenv("SIFT_VERIFY_WINDOW_MIN", "30")),
        fixture_verify_seconds=float(os.getenv("SIFT_FIXTURE_VERIFY_SECONDS", "8")),
        replay_speed=float(os.getenv("SIFT_REPLAY_SPEED", "1")),
        # Fixture data belongs to the demo environment, whatever DT_PLATFORM_URL says.
        env_id="abc12345" if mode == "fixture" else _env_id(platform_url),
        platform_url=platform_url if mode == "live" else "",
        web_dist=REPO_ROOT / "web" / "dist",
    )


def _env_id(platform_url: str) -> str:
    """`https://abc12345.apps.dynatrace.com` -> `abc12345`. Fixture default otherwise."""
    host = platform_url.split("//", 1)[-1].split("/", 1)[0]
    return host.split(".", 1)[0] if host else "abc12345"
