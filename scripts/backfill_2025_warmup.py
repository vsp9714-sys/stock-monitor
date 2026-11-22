#!/usr/bin/env python3
"""Backfill 2025 daily warmup history for Nifty 500 universe and benchmark index.

Ensures at least 210 completed daily sessions prior to 2026-01-01 so that
EMA 50x200, ADX(14), ATR(14), and 120-day Relative Strength are 100% warmed up
on Day 1 of the 2026 backtest window.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import logging
import sys
from pathlib import Path
import pandas as pd
from sqlalchemy import case, func

from src.config_loader import load_config
from src.db import (
    OHLCVCache,
    MarketContextCache,
    Stock,
    get_session,
    upsert_ohlcv_rows,
)
from src.fetcher.upstox import UpstoxFetcher
from src.market_calendar import IST, is_valid_candle_day
from src.scanner import _daily_timestamp
from src.universe import UniverseSelector

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backfill_2025")


def audit_cache(session, universe_symbols: list[str]) -> dict:
    """Audit current coverage of benchmark and universe symbols."""
    bench = session.query(MarketContextCache).filter_by(context_key="NSE_INDEX|Nifty 50").first()
    bench_info = {"count": 0, "min_date": None, "max_date": None}
    if bench and bench.candles_json:
        try:
            rows = json.loads(bench.candles_json)
            if rows:
                bench_info = {
                    "count": len(rows),
                    "min_date": str(rows[0][0])[:10],
                    "max_date": str(rows[-1][0])[:10],
                }
        except Exception:
            pass

    stats = (
        session.query(
            OHLCVCache.symbol,
            func.min(OHLCVCache.ts).label("min_ts"),
            func.max(OHLCVCache.ts).label("max_ts"),
            func.count(OHLCVCache.id).label("cnt"),
            func.sum(case((OHLCVCache.ts < datetime(2026, 1, 1), 1), else_=0)).label("pre_2026_cnt"),
        )
        .filter(
            OHLCVCache.interval == "1d",
            OHLCVCache.symbol.in_(universe_symbols),
        )
        .group_by(OHLCVCache.symbol)
        .all()
    )

    stats_by_sym = {s.symbol: s for s in stats}
    already_warm = [s for s in stats if str(s.min_ts)[:10] <= "2025-02-15"]
    need_backfill = [s for s in stats if str(s.min_ts)[:10] > "2025-02-15"]
    missing = [sym for sym in universe_symbols if sym not in stats_by_sym]
    ge_200_pre_2026 = [s for s in stats if (s.pre_2026_cnt or 0) >= 200]

    return {
        "benchmark": bench_info,
        "total_universe": len(universe_symbols),
        "cached_symbols": len(stats),
        "warm_symbols": len(already_warm),
        "need_backfill_symbols": len(need_backfill),
        "missing_symbols": len(missing),
        "ge_200_pre_2026": len(ge_200_pre_2026),
        "stats_by_sym": stats_by_sym,
        "need_backfill_list": [s.symbol for s in need_backfill],
    }


def backfill_benchmark(session, fetcher: UpstoxFetcher) -> None:
    """Ensure NSE_INDEX|Nifty 50 covers 2025-02-01 through 2026-11-20."""
    logger.info("Auditing and backfilling benchmark index (NSE_INDEX|Nifty 50)...")
    bench = session.query(MarketContextCache).filter_by(context_key="NSE_INDEX|Nifty 50").first()
    existing_rows = []
    earliest_date = date(2026, 11, 20)
    if bench and bench.candles_json:
        try:
            existing_rows = json.loads(bench.candles_json)
            if existing_rows:
                earliest_date = datetime.fromisoformat(existing_rows[0][0].replace("Z", "+00:00")).date()
        except Exception:
            existing_rows = []

    target_start = date(2025, 2, 1)
    if earliest_date <= date(2025, 2, 15):
        logger.info("Benchmark index already warmed back to %s (%d sessions)", earliest_date, len(existing_rows))
        return

    to_date = (earliest_date - timedelta(days=1)).isoformat()
    from_date = target_start.isoformat()
    logger.info("Fetching Nifty 50 historical candles from %s to %s...", from_date, to_date)
    df = fetcher.get_historical_candles("NSE_INDEX|Nifty 50", "day", from_date=from_date, to_date=to_date)
    if df is None or df.empty:
        logger.warning("No candles returned for Nifty 50 for range %s to %s", from_date, to_date)
        return

    # Parse and merge
    new_rows = []
    for row in df.itertuples(index=False):
        ts = pd.Timestamp(row.ts)
        new_rows.append([ts.isoformat(), float(row.open), float(row.high), float(row.low), float(row.close), float(row.volume)])

    all_rows = new_rows + existing_rows
    # Deduplicate by ts and sort
    seen_ts = set()
    deduped = []
    for r in all_rows:
        t_str = str(r[0])
        if t_str not in seen_ts:
            seen_ts.add(t_str)
            deduped.append(r)
    deduped.sort(key=lambda r: r[0])

    if bench is None:
        bench = MarketContextCache(context_key="NSE_INDEX|Nifty 50", candles_json="[]", fetched_at=datetime.now(timezone.utc))
        session.add(bench)
    bench.candles_json = json.dumps(deduped)
    bench.fetched_at = datetime.now(timezone.utc)
    session.commit()
    logger.info("Benchmark index updated: now covers %d sessions (%s to %s)", len(deduped), deduped[0][0][:10], deduped[-1][0][:10])


def backfill_stocks(session, fetcher: UpstoxFetcher, cfg: dict, universe_symbols: list[str]) -> None:
    """Backfill active Nifty 500 stocks back to 2025-02-01."""
    stock_records = {s.symbol: s for s in session.query(Stock).filter(Stock.symbol.in_(universe_symbols)).all()}
    target_start = date(2025, 2, 1)

    market_cfg = cfg.get("market", {})
    pending_rows: list[dict] = []
    checkpoint_every = 20
    processed_count = 0
    shorter_history_symbols: list[tuple[str, str]] = []

    # Get current min_ts per symbol
    min_dates = dict(
        session.query(OHLCVCache.symbol, func.min(OHLCVCache.ts))
        .filter(OHLCVCache.interval == "1d", OHLCVCache.symbol.in_(universe_symbols))
        .group_by(OHLCVCache.symbol)
        .all()
    )

    symbols_to_process = []
    for sym in universe_symbols:
        min_ts = min_dates.get(sym)
        if min_ts is None or min_ts.date() > date(2025, 2, 15):
            symbols_to_process.append(sym)

    total_to_process = len(symbols_to_process)
    logger.info("Total universe symbols needing 2025 backfill: %d / %d", total_to_process, len(universe_symbols))
    if total_to_process == 0:
        logger.info("All universe symbols are already warmed up back to <= 2025-02-15.")
        return

    for idx, sym in enumerate(symbols_to_process, 1):
        stock = stock_records.get(sym)
        if not stock or not stock.instrument_key:
            logger.warning("No instrument key found for %s; skipping", sym)
            continue

        min_ts = min_dates.get(sym)
        if min_ts is not None:
            earliest_date = min_ts.date()
            to_date = earliest_date - timedelta(days=1)
        else:
            to_date = date(2026, 11, 20)

        if to_date < target_start:
            continue

        from_date = target_start
        try:
            df = fetcher.get_historical_candles(
                stock.instrument_key,
                interval="day",
                from_date=from_date.isoformat(),
                to_date=to_date.isoformat(),
            )
            if df is None or df.empty:
                logger.info("[%d/%d] %s: No historical bars returned for %s to %s (possible recent IPO)", idx, total_to_process, sym, from_date, to_date)
                shorter_history_symbols.append((sym, "no_bars"))
                continue

            symbol_rows = []
            for row in df.itertuples(index=False):
                candle_ts = _daily_timestamp(row.ts)
                if not is_valid_candle_day(candle_ts.date(), market_cfg):
                    continue
                symbol_rows.append({
                    "symbol": sym,
                    "interval": "1d",
                    "ts": candle_ts,
                    "open": float(row.open),
                    "high": float(row.high),
                    "low": float(row.low),
                    "close": float(row.close),
                    "volume": float(row.volume),
                })

            if symbol_rows:
                pending_rows.extend(symbol_rows)
                first_date = min(r["ts"].date() for r in symbol_rows)
                if first_date > date(2025, 2, 15):
                    shorter_history_symbols.append((sym, first_date.isoformat()))

            processed_count += 1
            if processed_count % checkpoint_every == 0:
                inserted = upsert_ohlcv_rows(session, pending_rows, commit=True)
                logger.info(
                    "Checkpoint [%d/%d] (%.1f%%): Committed %d rows across last %d symbols",
                    idx, total_to_process, (idx / total_to_process) * 100.0, len(pending_rows), checkpoint_every,
                )
                pending_rows.clear()
            elif idx % 25 == 0 or idx == total_to_process:
                logger.info("Progress [%d/%d] (%.1f%%): %s fetched %d bars", idx, total_to_process, (idx / total_to_process) * 100.0, sym, len(symbol_rows))

        except Exception as exc:
            logger.error("Error fetching historical bars for %s: %s", sym, exc)

    if pending_rows:
        upsert_ohlcv_rows(session, pending_rows, commit=True)
        logger.info("Final checkpoint: Committed %d remaining rows", len(pending_rows))
        pending_rows.clear()

    if shorter_history_symbols:
        logger.info("Naturally shorter history / IPO symbols (%d total): %s", len(shorter_history_symbols), shorter_history_symbols[:15])


def main():
    cfg = load_config()
    session = get_session()
    fetcher = UpstoxFetcher()

    selector = UniverseSelector(cfg, get_session)
    candidates = session.query(Stock).filter_by(is_active=True).all()
    result = selector.resolve(candidates, now=datetime.now(IST))
    universe_symbols = [s.symbol if hasattr(s, "symbol") else str(s) for s in result.symbols]

    logger.info("============================================================")
    logger.info(" PRE-FLIGHT AUDIT: NIFTY 500 CACHE STATUS")
    logger.info("============================================================")
    pre_audit = audit_cache(session, universe_symbols)
    logger.info("Nifty 50 Benchmark: %s (%s to %s)", pre_audit["benchmark"]["count"], pre_audit["benchmark"]["min_date"], pre_audit["benchmark"]["max_date"])
    logger.info("Universe Size: %d symbols", pre_audit["total_universe"])
    logger.info("Cached Symbols: %d", pre_audit["cached_symbols"])
    logger.info("Warm Symbols (min_ts <= 2025-02-15): %d", pre_audit["warm_symbols"])
    logger.info("Needing Backfill (min_ts > 2025-02-15): %d", pre_audit["need_backfill_symbols"])
    logger.info("Symbols with >= 200 pre-2026 sessions: %d", pre_audit["ge_200_pre_2026"])
    logger.info("============================================================\n")

    # Step 1: Benchmark
    backfill_benchmark(session, fetcher)

    # Step 2: Universe Stocks
    backfill_stocks(session, fetcher, cfg, universe_symbols)

    logger.info("\n============================================================")
    logger.info(" POST-BACKFILL AUDIT: NIFTY 500 CACHE STATUS")
    logger.info("============================================================")
    post_audit = audit_cache(session, universe_symbols)
    logger.info("Nifty 50 Benchmark: %s sessions (%s to %s)", post_audit["benchmark"]["count"], post_audit["benchmark"]["min_date"], post_audit["benchmark"]["max_date"])
    logger.info("Universe Size: %d symbols", post_audit["total_universe"])
    logger.info("Warm Symbols (min_ts <= 2025-02-15): %d", post_audit["warm_symbols"])
    logger.info("Needing Backfill: %d", post_audit["need_backfill_symbols"])
    logger.info("Symbols with >= 200 pre-2026 sessions: %d / %d (%.1f%%)", post_audit["ge_200_pre_2026"], post_audit["total_universe"], (post_audit["ge_200_pre_2026"] / post_audit["total_universe"]) * 100.0)
    logger.info("============================================================")


if __name__ == "__main__":
    main()
