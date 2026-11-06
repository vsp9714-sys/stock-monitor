"""Run the readiness-gated daily cache refresh once, including on weekends.

This maintenance entry point does not execute a signal scan or invoke notifiers.
It is intended for an explicitly scheduled/manual cache-only refresh.
"""
from __future__ import annotations

import json
import logging

from src.db import get_engine
from src.scanner import Scanner


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    get_engine()
    result = Scanner().refresh_latest_completed_daily_cache(manual=True)
    print(json.dumps(result, sort_keys=True, default=str))
    # A readiness skip is a valid safe outcome, but return nonzero so systemd
    # shows that no full-universe refresh happened and operators can inspect it.
    return 2 if result.get("skipped") else 0


if __name__ == "__main__":
    raise SystemExit(main())
