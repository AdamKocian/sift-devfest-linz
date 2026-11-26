"""Command line: `uv run python -m server.cli reset` rebuilds the demo database."""

from __future__ import annotations

import sys

from server.config import load_config
from server.db import make_engine, utcnow
from server.fixtures import seed


def main(argv: list[str]) -> int:
    if argv[:1] != ["reset"]:
        print("usage: python -m server.cli reset", file=sys.stderr)
        return 2
    cfg = load_config()
    seed(make_engine(cfg.db_path), cfg, utcnow())
    print(f"Demo data reset: {cfg.db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
