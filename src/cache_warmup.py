"""Resumable one-time daily OHLCV cache warm-up.

Run with: python -m src.cache_warmup [--limit N]
Completed symbols are detected from SQLite, so rerunning safely resumes work.
"""
import argparse
import logging
import sys

from src.db import get_engine
from src.scanner import Scanner


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, help="warm only the first N active stocks (smoke checks)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    get_engine()
    result = Scanner().warm_candle_cache(limit=args.limit)
    print(
        "Cache warm-up: "
        f"universe={result['universe_size']} warmed={result['warmed']} "
        f"already_complete={result['already_complete']} failed={result['failed']} "
        f"insufficient_history={result['insufficient_history']} "
        f"minimum_daily_candles={result['minimum_daily_candles']}"
    )
    if result["failed"]:
        print("Failed symbols: " + ", ".join(result["failed_symbols"][:50]))
        return 1
    if result["insufficient_history"]:
        print(
            "Symbols with insufficient history for every configured check: "
            + ", ".join(result["insufficient_history_symbols"][:50])
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
