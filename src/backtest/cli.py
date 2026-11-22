"""Generate offline JSON/CSV/text reports from the local completed-candle cache."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import date, timedelta, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from src.config_loader import get_config
from src.db import MarketContextCache, OHLCVCache, Stock, UniverseScanSnapshot, get_session
from src.scanner import Scanner
from src.backtest.engine import benchmark_return, simulate_period
from src.market_context import average_traded_value, compute_universe_rs_ratings, regime_for_candles


def _bars_from_rows(rows) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    for row in rows:
        result.setdefault(row.symbol, []).append({
            "ts": row.ts, "open": row.open, "high": row.high, "low": row.low,
            "close": row.close, "volume": row.volume,
        })
    return result


def _read_market_cache(cache: MarketContextCache | None):
    if cache is None:
        return None
    try:
        import pandas as pd
        values = json.loads(cache.candles_json)
        return pd.DataFrame(values, columns=["ts", "open", "high", "low", "close", "volume"])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _summarize(result: dict) -> str:
    summary = result["summary"]
    return (
        f"Trades: {summary['trade_count']} (wins {summary['wins']}, losses {summary['losses']}); "
        f"win rate: {summary['win_rate_pct'] if summary['win_rate_pct'] is not None else 'n/a'}%; "
        f"net expectancy: {summary['expectancy_net_return_pct_per_trade'] if summary['expectancy_net_return_pct_per_trade'] is not None else 'n/a'}%/trade; "
        f"max drawdown (sequential closed-trade curve): {summary['max_drawdown_pct_sequential_trade_equity']:.2f}%; "
        f"exposure: {summary['exposure_pct_symbol_sessions'] if summary['exposure_pct_symbol_sessions'] is not None else 'n/a'}% symbol-sessions."
    )


def run_backtest(start_date: date, end_date: date, symbols: list[str] | None, out_dir: Path) -> dict:
    if start_date >= end_date:
        raise ValueError("--from-date must be earlier than --to-date")
    cfg = get_config()
    session = get_session()
    try:
        if not symbols:
            symbols = [row.symbol for row in session.query(Stock.symbol).filter_by(is_active=True).order_by(Stock.symbol).all()]
        warmup_start = start_date - timedelta(days=365)
        rows = session.query(OHLCVCache).filter(
            OHLCVCache.interval == "1d", OHLCVCache.symbol.in_(symbols),
            OHLCVCache.ts >= datetime.combine(warmup_start, datetime.min.time()),
            OHLCVCache.ts <= datetime.combine(end_date, datetime.max.time()),
        ).order_by(OHLCVCache.symbol, OHLCVCache.ts).all()
        import pandas as pd
        candles = {symbol: pd.DataFrame(values).sort_values("ts").reset_index(drop=True)
                   for symbol, values in _bars_from_rows(rows).items()}

        snapshots: dict[date, set[str]] = {}
        snapshot_rows = session.query(UniverseScanSnapshot).filter(
            UniverseScanSnapshot.snapshot_date >= start_date,
            UniverseScanSnapshot.snapshot_date <= end_date,
        ).order_by(UniverseScanSnapshot.created_at).all()
        for snapshot in snapshot_rows:
            snapshots[snapshot.snapshot_date] = set(json.loads(snapshot.symbols_json))
        membership = snapshots or None
        benchmark_key = cfg.get("market_context", {}).get("benchmark_key", "NSE_INDEX|Nifty 50")
        benchmark_cache = session.query(MarketContextCache).filter_by(context_key=benchmark_key).first()
        benchmark = _read_market_cache(benchmark_cache)
    finally:
        session.close()

    evaluator = object.__new__(Scanner)
    evaluator.cfg = cfg
    evaluator.signal_cfg = cfg.get("signals", {})
    evaluator.universe_cfg = cfg.get("universe", {})
    evaluator.confluence_cfg = cfg.get("confluence", {})
    evaluator.guardrails_cfg = cfg.get("guardrails", {})

    market_context_cfg = cfg.get("market_context", {}) or {}
    if market_context_cfg.get("mode", "informational") == "filter":
        raise ValueError(
            "Backtest cannot reproduce market_context.mode=filter without point-in-time "
            "sector/fundamental mappings; use informational mode for a faithful replay."
        )

    guardrails_cfg = cfg.get("guardrails", {}) or {}
    rs_filter_cfg = guardrails_cfg.get("rs_filter", {}) or {}

    # Precompute benchmark regimes by date using completed benchmark candles ts <= d
    benchmark_regimes_by_date: dict[date, dict] = {}
    if benchmark is not None and not benchmark.empty:
        b_short = int(market_context_cfg.get("short_sma_sessions", 20))
        b_long = int(market_context_cfg.get("long_sma_sessions", 50))
        b_df = benchmark.copy().sort_values("ts").drop_duplicates(subset=["ts"], keep="last").reset_index(drop=True)
        b_dates = [pd.Timestamp(t).date() for t in b_df["ts"]]
        unique_b_dates = sorted({d for d in b_dates if d is not None})
        for d in unique_b_dates:
            if d <= end_date:
                indices = [idx for idx, bd in enumerate(b_dates) if bd <= d]
                b_prefix = b_df.iloc[indices]
                benchmark_regimes_by_date[d] = regime_for_candles(b_prefix, short_window=b_short, long_window=b_long)

    # Precompute universe cross-sectional RS rating for each session date
    all_days = sorted({
        pd.Timestamp(ts).date()
        for frame in candles.values() for ts in frame["ts"]
        if start_date <= pd.Timestamp(ts).date() <= end_date
    })
    rs_ratings_by_date: dict[date, dict[str, int]] = {}
    if benchmark is not None and not benchmark.empty:
        for d in all_days:
            active_symbols = membership.get(d) if (membership and d in membership) else None
            if active_symbols is not None:
                sub_stocks = {s: candles[s] for s in active_symbols if s in candles}
            else:
                sub_stocks = candles
            rs_ratings_by_date[d] = compute_universe_rs_ratings(
                sub_stocks, benchmark, as_of_date=d, lookback_weights=rs_filter_cfg.get("lookback_weights")
            )

    def evaluate(symbol: str, frame):
        tradability = (cfg.get("universe", {}) or {}).get("tradability", {}) or {}
        if tradability.get("enabled", False):
            metric = average_traded_value(
                frame,
                sessions=int(tradability.get("average_traded_value_sessions", 20)),
                minimum_observations=int(tradability.get(
                    "minimum_observations",
                    tradability.get("average_traded_value_sessions", 20),
                )),
            )
            if metric.get("status") != "available":
                if tradability.get("unknown_policy", "exclude") != "include":
                    return None
            elif metric["average_inr"] < float(
                tradability.get("minimum_average_traded_value_inr", 10_000_000)
            ):
                return None
        stock = SimpleNamespace(symbol=symbol, company_name=symbol)
        result, _ = Scanner._scan_stock(evaluator, stock, frame, provisional=False)
        if not result:
            return None

        signal_day = pd.Timestamp(frame.iloc[-1]["ts"]).date()
        bench_res = benchmark_regimes_by_date.get(signal_day, {})
        bench_regime = bench_res.get("regime") if bench_res.get("status") == "available" else None
        result["benchmark_regime_at_signal"] = bench_regime

        rs_val = rs_ratings_by_date.get(signal_day, {}).get(symbol)
        result["rs_rating_at_signal"] = rs_val

        # Benchmark Regime Gate
        if market_context_cfg.get("mode") == "benchmark_filter":
            allowed_buy = market_context_cfg.get("allowed_buy_regimes", ["bullish", "mixed"])
            allowed_sell = market_context_cfg.get("allowed_sell_regimes", ["bearish", "mixed"])
            unknown_policy = market_context_cfg.get("filter_unknown_policy", "exclude")
            if bench_regime is None:
                if unknown_policy == "exclude":
                    return None
            else:
                if result["direction"] == "BUY" and bench_regime not in allowed_buy:
                    return None
                if result["direction"] == "SELL" and bench_regime not in allowed_sell:
                    return None

        # Cross-Sectional RS Filter
        if rs_filter_cfg.get("enabled", False):
            min_buy = int(rs_filter_cfg.get("min_rs_rating_buy", 60))
            max_sell = int(rs_filter_cfg.get("max_rs_rating_sell", 40))
            unknown_policy = rs_filter_cfg.get("unknown_policy", "exclude")
            if rs_val is None:
                if unknown_policy == "exclude":
                    return None
            else:
                if result["direction"] == "BUY" and rs_val < min_buy:
                    return None
                if result["direction"] == "SELL" and rs_val > max_sell:
                    return None

        return result

    bt_cfg = cfg.get("backtest", {})
    train_fraction = float(bt_cfg.get("train_fraction", 0.7))
    if not 0.5 <= train_fraction < 1.0:
        raise ValueError("backtest.train_fraction must be in [0.5, 1)")
    total = simulate_period(candles, evaluate, start_date=start_date, end_date=end_date,
                            config=cfg, membership_by_date=membership)
    if len(all_days) >= 4:
        split = min(len(all_days) - 1, max(1, int(len(all_days) * train_fraction)))
        train_end, test_start = all_days[split - 1], all_days[split]
        train = simulate_period(candles, evaluate, start_date=start_date, end_date=train_end,
                                config=cfg, membership_by_date=membership)
        out_of_sample = simulate_period(candles, evaluate, start_date=test_start, end_date=end_date,
                                        config=cfg, membership_by_date=membership)
        walk_forward = {"status": "available", "train_end": train_end.isoformat(),
                        "out_of_sample_start": test_start.isoformat(),
                        "train": train["summary"], "out_of_sample": out_of_sample["summary"]}
    else:
        walk_forward = {"status": "unavailable", "reason": "fewer_than_four_sessions"}

    strategy_cfg = {key: cfg.get(key) for key in ("signals", "confluence", "guardrails", "backtest", "risk", "market_context")}
    config_hash = hashlib.sha256(json.dumps(strategy_cfg, sort_keys=True, default=str).encode()).hexdigest()
    limits = [
        "Only locally cached daily OHLCV is used; the command performs no network requests and sends no alerts.",
        "Current symbols/universe may have survivorship bias; point-in-time membership is applied only on dates with a recorded universe snapshot.",
        "Corporate-action treatment follows existing cached data; split adjustments are not added by Batch B decision.",
        "The completed-session tradability screen is evaluated at each signal close using only prior/current completed bars.",
        "Optional market/sector context is informational in this replay; filter mode is rejected because historical sector mappings are not point-in-time cached.",
        "Sequential closed-trade drawdown compounds each trade return and is not a simultaneous multi-position portfolio drawdown.",
        "Fees/slippage are fixed basis-point assumptions and exclude market impact, taxes not configured here, and execution uncertainty.",
    ]
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "period": {"from": start_date.isoformat(), "to": end_date.isoformat()},
        "symbols_requested": sorted(symbols), "symbols_with_data": sorted(candles),
        "config_hash_sha256": config_hash,
        "source": "SQLite OHLCVCache interval=1d; completed candles only",
        "assumptions": {
            "signal": "live configured daily strategy evaluated on data through the signal close",
            "entry": "next available session open",
            "stop": f"{cfg.get('backtest', {}).get('stop_atr_multiple', cfg.get('risk', {}).get('stop_atr_multiple', 1.5))} x ATR({cfg.get('backtest', {}).get('atr_period', cfg.get('risk', {}).get('atr_period', 14))})",
            "target": f"{cfg.get('backtest', {}).get('target_r_multiple', cfg.get('risk', {}).get('target_r_multiple', 2.0))}R",
            "same_bar_collision": "stop first",
            "gap_stop": "fill at open when open has gapped beyond stop",
            "costs": {"fees_bps_per_side": bt_cfg.get("fee_bps_per_side", 5.0),
                      "slippage_bps_per_side": bt_cfg.get("slippage_bps_per_side", 5.0)},
            "trailing_stop_mode": bt_cfg.get("trailing_stop_mode", "none"),
            "benchmark_filter": {
                "enabled": market_context_cfg.get("mode") == "benchmark_filter",
                "mode": market_context_cfg.get("mode", "informational"),
                "allowed_buy_regimes": market_context_cfg.get("allowed_buy_regimes", ["bullish", "mixed"]),
                "allowed_sell_regimes": market_context_cfg.get("allowed_sell_regimes", ["bearish", "mixed"]),
            },
            "rs_filter": rs_filter_cfg,
            "concurrent_positions": "not modeled; drawdown is sequential closed-trade equity",
            "universe_membership_snapshots_found": len(snapshots),
        },
        "overall": total["summary"], "walk_forward": walk_forward,
        "benchmark": benchmark_return(benchmark, start_date, end_date),
        "limitations": limits,
        "trades": total["trades"],
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(report, indent=2, default=str) + "\n")
    if total["trades"]:
        with (out_dir / "trades.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(total["trades"][0]))
            writer.writeheader()
            writer.writerows(total["trades"])
    else:
        (out_dir / "trades.csv").write_text("symbol,direction,signal_date,entry_date,entry_price,exit_date,exit_price,exit_reason,net_return_pct\n")
    text = [f"Stock Monitor offline backtest: {start_date} through {end_date}", _summarize(total),
            f"Benchmark: {report['benchmark']}", f"Walk-forward: {walk_forward['status']}", "Limitations:"]
    text.extend(f"- {item}" for item in limits)
    (out_dir / "summary.txt").write_text("\n".join(text) + "\n")
    return {
        "output_dir": str(out_dir),
        "overall": total["summary"],
        "walk_forward": walk_forward,
        "config_hash_sha256": config_hash,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run an offline stock-signal backtest from SQLite daily candles.")
    parser.add_argument("--from-date", required=True, type=date.fromisoformat)
    parser.add_argument("--to-date", required=True, type=date.fromisoformat)
    parser.add_argument("--symbols", help="Comma-separated symbols; defaults to active universe")
    parser.add_argument("--out-dir", default=f"reports/backtests/{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}")
    args = parser.parse_args()
    symbols = [value.strip().upper() for value in args.symbols.split(",") if value.strip()] if args.symbols else None
    print(json.dumps(run_backtest(args.from_date, args.to_date, symbols, Path(args.out_dir)), indent=2))


if __name__ == "__main__":
    main()
