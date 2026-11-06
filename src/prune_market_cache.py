"""Prune market-data caches to the configured universe, with a backup first.

Dry-run by default. Pass --execute only when the printed symbol count is correct.
The SQLite backup preserves the complete pre-prune database for rollback.
"""
import argparse
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from src.config_loader import get_config
from src.db import get_engine, get_session, prune_market_data_to_symbols
from src.scanner import Scanner


def _backup_database(db_path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = db_path.with_name(f"{db_path.name}.pre-universe-prune-{stamp}.bak")
    with sqlite3.connect(db_path) as source, sqlite3.connect(backup_path) as destination:
        source.backup(destination)
    return backup_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="back up then prune non-selected market caches")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    get_engine()
    scanner = Scanner()
    selected = scanner.get_active_stocks()
    symbols = {stock.symbol for stock in selected}
    if not symbols:
        raise SystemExit("Refusing to prune: selected universe is empty")
    session = get_session()
    try:
        from sqlalchemy import func
        from src.db import DailyCandleRefresh, Fundamental, OHLCVCache
        totals = {
            "ohlcv_cache": session.query(func.count(OHLCVCache.id)).filter(~OHLCVCache.symbol.in_(symbols)).scalar(),
            "daily_candle_refresh": session.query(func.count(DailyCandleRefresh.symbol)).filter(~DailyCandleRefresh.symbol.in_(symbols)).scalar(),
            "fundamentals": session.query(func.count(Fundamental.id)).filter(~Fundamental.symbol.in_(symbols)).scalar(),
        }
        print(f"Selected symbols: {len(symbols)}; excluded market-data rows: {totals}")
        if not args.execute:
            print("Dry run only; pass --execute to create a backup and prune.")
            return 0
        db_path = Path(get_config()["database"]["path"]).resolve()
        backup_path = _backup_database(db_path)
        counts = prune_market_data_to_symbols(session, symbols)
        print(f"Backup: {backup_path}; deleted rows: {counts}")
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
