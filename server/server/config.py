"""Runtime configuration, read once from environment variables.

SIFT_MODE says where Dynatrace data comes from (fixture recordings or a live tenant; the
API server supports fixture only so far). SIFT_AGENT says who does the agent's work:
`replay` re-runs recorded Gemini runs from fixtures/agent/ (offline, no Gemini calls),
`gemini` runs the real ADK agent on Vertex AI (over the fixture connectors when
SIFT_MODE=fixture).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

APPLY_TARGETS = ("dry-run", "openpipeline", "otelcol")
AGENTS = ("replay", "gemini")


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
    agent: str = "replay"
    gcp_project: str = ""
    gcp_location: str = "global"
    gemini_model: str = ""
    gemini_fast_model: str = ""
    gemini_max_tokens_per_run: int = 600_000
    gemini_max_llm_calls: int = 40
    otelcol_bin: str | None = None
    dt_mcp_url: str = ""
    dt_platform_token: str = ""

    @property
    def is_fixture(self) -> bool:
        return self.mode == "fixture"


def load_config() -> Config:
    mode = os.getenv("SIFT_MODE", "fixture").strip() or "fixture"
    if mode not in ("fixture", "live"):
        raise ValueError(f"SIFT_MODE must be 'fixture' or 'live', got {mode!r}")
    if mode == "live":
        raise NotImplementedError(
            "SIFT_MODE=live (the API server on a tenant) is not implemented yet: no tenant to "
            "test it on. Use SIFT_MODE=fixture (SIFT_AGENT=gemini runs the real agent over the "
            "fixture data) and `make discover-live` for tenant discovery."
        )
    target = os.getenv("SIFT_APPLY_TARGET", "dry-run").strip() or "dry-run"
    if target not in APPLY_TARGETS:
        raise ValueError(f"SIFT_APPLY_TARGET must be one of {APPLY_TARGETS}, got {target!r}")
    platform_url = os.getenv("DT_PLATFORM_URL", "").strip()
    agent = os.getenv("SIFT_AGENT", "replay").strip() or "replay"
    if agent not in AGENTS:
        raise ValueError(f"SIFT_AGENT must be one of {AGENTS}, got {agent!r}")
    model = os.getenv("GEMINI_MODEL", "").strip()
    fast = os.getenv("GEMINI_FAST_MODEL", "").strip()
    project = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
    if agent == "gemini" and not (model and fast and project):
        raise ValueError(
            "SIFT_AGENT=gemini needs GOOGLE_CLOUD_PROJECT, GEMINI_MODEL and GEMINI_FAST_MODEL"
        )
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
        agent=agent,
        gcp_project=project,
        gcp_location=os.getenv("GOOGLE_CLOUD_LOCATION", "global").strip() or "global",
        gemini_model=model,
        gemini_fast_model=fast,
        gemini_max_tokens_per_run=int(os.getenv("SIFT_GEMINI_MAX_TOKENS_PER_RUN", "600000")),
        gemini_max_llm_calls=int(os.getenv("SIFT_GEMINI_MAX_LLM_CALLS", "40")),
        otelcol_bin=os.getenv("SIFT_OTELCOL_BIN", "").strip() or None,
        dt_mcp_url=os.getenv("DT_MCP_URL", "").strip(),
        dt_platform_token=os.getenv("DT_PLATFORM_TOKEN", "").strip(),
    )


def _env_id(platform_url: str) -> str:
    """`https://abc12345.apps.dynatrace.com` -> `abc12345`. Fixture default otherwise."""
    host = platform_url.split("//", 1)[-1].split("/", 1)[0]
    return host.split(".", 1)[0] if host else "abc12345"
