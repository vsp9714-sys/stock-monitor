"""Run the morning daily-candle readiness check once, without starting the service.

Examples:
  python -m src.refresh_daily_cache --manual --universe nifty500
  python -m src.refresh_daily_cache  # only when inside the configured 06:00-07:00 window

This command refreshes OHLCV only; it never runs a scan or sends notifications.
"""
import argparse
import json
import logging
import sys

from src.db import get_engine
from src.scanner import Scanner


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manual", action="store_true",
        help="allow a one-shot readiness check outside the scheduled 06:00-07:00 IST window",
    )
    parser.add_argument(
        "--universe", choices=("all_nse", "nifty500", "custom", "composed"),
        help="override the configured universe for this command only",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    get_engine()
    scanner = Scanner()
    if args.universe:
        scanner.universe_cfg["mode"] = args.universe
    result = scanner.refresh_latest_completed_daily_cache(manual=args.manual)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result.get("skipped") is False else 2 if result.get("skipped") else 1


if __name__ == "__main__":
    sys.exit(main())
