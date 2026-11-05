"""Run one manual quote-based scan and report whether the universe was complete.

This command uses normal escalation deduplication and configured notification
routes. Run only when an alert sweep is intended.
"""
import json
import logging
import sys
from dataclasses import asdict

from src.db import get_engine
from src.scanner import Scanner


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    get_engine()
    summary = Scanner().run_scan()
    print(json.dumps(asdict(summary), sort_keys=True))
    return 0 if summary.complete else 2


if __name__ == "__main__":
    sys.exit(main())
