"""Unit tests for Phase 3A enhancements:
1. No-lookahead Benchmark Regime Gate
2. No-lookahead Cross-Sectional RS Rating 1..99
3. No-lookahead Trailing Stop (breakeven_1r & atr_trail)
"""
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
import pytest
import pandas as pd
import numpy as np

from src.market_context import (
    regime_for_candles,
    compute_stock_composite_rs,
    compute_universe_rs_ratings,
)
from src.backtest.engine import simulate_period
from src.backtest.cli import run_backtest


def _make_daily_frame(dates: list[date], closes: list[float], highs=None, lows=None, opens=None, volumes=None) -> pd.DataFrame:
    n = len(dates)
    closes_arr = np.array(closes, dtype=float)
    highs_arr = np.array(highs if highs is not None else closes_arr * 1.01, dtype=float)
    lows_arr = np.array(lows if lows is not None else closes_arr * 0.99, dtype=float)
    opens_arr = np.array(opens if opens is not None else closes_arr, dtype=float)
    vols_arr = np.array(volumes if volumes is not None else [100000.0] * n, dtype=float)
    return pd.DataFrame({
        "ts": [pd.Timestamp(d) for d in dates],
        "open": opens_arr,
        "high": highs_arr,
        "low": lows_arr,
        "close": closes_arr,
        "volume": vols_arr,
    })


# ─────────────────────────────────────────────────────────────────────────────
# 1. Benchmark Regime Gate Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_regime_for_candles_no_lookahead_and_classification():
    # 60 sessions
    base_dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(70)]
    
    # First 50 days uptrend: 100 -> 150
    # Next 20 days sharp downtrend: 150 -> 80
    closes = [100.0 + i for i in range(50)] + [150.0 - (i * 3.5) for i in range(1, 21)]
    bench_df = _make_daily_frame(base_dates, closes)

    # Point-in-time check on day 49 (end of uptrend)
    df_day49 = bench_df.iloc[:50]
    regime_day49 = regime_for_candles(df_day49, short_window=20, long_window=50)
    assert regime_day49["status"] == "available"
    assert regime_day49["regime"] == "bullish"
    assert regime_day49["close"] == 149.0

    # Point-in-time check on day 69 (after crash)
    df_day69 = bench_df.iloc[:70]
    regime_day69 = regime_for_candles(df_day69, short_window=20, long_window=50)
    assert regime_day69["status"] == "available"
    assert regime_day69["regime"] == "bearish"

    # Future bars added to bench_df do NOT alter the evaluation of df_day49
    future_dates = [date(2026, 3, 15) + timedelta(days=i) for i in range(30)]
    extended_closes = closes + [50.0] * 30
    extended_df = _make_daily_frame(base_dates + future_dates, extended_closes)
    regime_prefix = regime_for_candles(extended_df.iloc[:50], short_window=20, long_window=50)
    assert regime_prefix["regime"] == regime_day49["regime"]
    assert regime_prefix["close"] == regime_day49["close"]


def test_backtest_cli_rejects_mode_filter():
    cfg = {"market_context": {"mode": "filter"}}
    import src.backtest.cli as cli_mod
    orig_get_config = cli_mod.get_config
    cli_mod.get_config = lambda: cfg
    try:
        with pytest.raises(ValueError, match="Backtest cannot reproduce market_context.mode=filter"):
            run_backtest(date(2026, 6, 1), date(2026, 6, 10), symbols=None, out_dir=Path("/tmp/test_bt_out"))
    finally:
        cli_mod.get_config = orig_get_config


# ─────────────────────────────────────────────────────────────────────────────
# 2. Cross-Sectional RS Rating Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_compute_stock_composite_rs_and_fallbacks():
    dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(140)]
    bench_df = _make_daily_frame(dates, [100.0 + i * 0.2 for i in range(140)])
    
    # Stock with 140 days (full 120, 60, 20 lookbacks available)
    stock_full = _make_daily_frame(dates, [100.0 + i * 0.5 for i in range(140)])
    res_full = compute_stock_composite_rs(stock_full, bench_df, as_of_date=dates[-1])
    assert res_full["status"] == "available"
    assert res_full["available_lookbacks"] == [20, 60, 120]
    assert res_full["composite_score"] > 0

    # Stock with 70 days (< 121 sessions): drops 120 lookback, re-normalizes over [20, 60]
    stock_mid = _make_daily_frame(dates[-70:], [100.0 + i * 0.5 for i in range(70)])
    res_mid = compute_stock_composite_rs(stock_mid, bench_df, as_of_date=dates[-1])
    assert res_mid["status"] == "available"
    assert res_mid["available_lookbacks"] == [20, 60]
    assert pytest.approx(sum(res_mid["normalized_weights"].values()), 1e-6) == 1.0

    # Stock with 25 days (< 61 sessions): drops 120 and 60, re-normalizes over [20]
    stock_short = _make_daily_frame(dates[-25:], [100.0 + i * 0.5 for i in range(25)])
    res_short = compute_stock_composite_rs(stock_short, bench_df, as_of_date=dates[-1])
    assert res_short["status"] == "available"
    assert res_short["available_lookbacks"] == [20]
    assert res_short["normalized_weights"][20] == 1.0

    # Stock with 15 days (< 21 sessions): unavailable
    stock_tiny = _make_daily_frame(dates[-15:], [100.0 + i * 0.5 for i in range(15)])
    res_tiny = compute_stock_composite_rs(stock_tiny, bench_df, as_of_date=dates[-1])
    assert res_tiny["status"] == "unavailable"
    assert res_tiny["reason"] == "insufficient_common_sessions"


def test_universe_rs_ratings_ranking_and_no_lookahead():
    dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(100)]
    bench_df = _make_daily_frame(dates, [100.0] * 100)

    # 4 stocks
    # S1: huge gainer (+100%) -> Top
    # S2: moderate gainer (+50%)
    # S3: slight loser (-10%)
    # S4: huge loser (-50%) -> Worst
    stocks = {
        "S1": _make_daily_frame(dates, [100.0 + i * 1.0 for i in range(100)]),
        "S2": _make_daily_frame(dates, [100.0 + i * 0.5 for i in range(100)]),
        "S3": _make_daily_frame(dates, [100.0 - i * 0.1 for i in range(100)]),
        "S4": _make_daily_frame(dates, [100.0 - i * 0.5 for i in range(100)]),
    }

    as_of = dates[80]
    ratings = compute_universe_rs_ratings(stocks, bench_df, as_of_date=as_of)
    assert len(ratings) == 4
    assert ratings["S1"] == 99  # Top performer gets 99
    assert ratings["S4"] == 1   # Worst performer gets 1
    assert ratings["S1"] > ratings["S2"] > ratings["S3"] > ratings["S4"]

    # Verify no-lookahead: add future volatile sessions (dates[81..99])
    # Even if S4 moons on day 95, as_of day 80 rating MUST not change!
    stocks_mutated = {
        k: df.copy() for k, df in stocks.items()
    }
    stocks_mutated["S4"].loc[85:, "close"] = 10000.0  # huge future pump
    ratings_post = compute_universe_rs_ratings(stocks_mutated, bench_df, as_of_date=as_of)
    assert ratings_post == ratings


# ─────────────────────────────────────────────────────────────────────────────
# 3. Trailing Stop Tests (Engine)
# ─────────────────────────────────────────────────────────────────────────────

def test_trailing_stop_breakeven_1r_no_intraday_lookahead():
    """Verify bar hitting +1.0R intraday and crashing on the SAME bar uses the initial stop."""
    dates = [date(2026, 6, 1) + timedelta(days=i) for i in range(40)]
    
    # 35 warmup bars so ATR works, then signal on bar 35 (entry on bar 36)
    closes = [100.0] * 35 + [100.0, 100.0, 100.0, 100.0, 100.0]
    highs = [102.0] * 35 + [102.0, 115.0, 105.0, 105.0, 105.0]
    lows = [98.0] * 35 + [98.0, 85.0, 95.0, 95.0, 95.0]
    opens = [100.0] * 40
    
    # On bar 36 (entry_idx = 36):
    # Entry at open = 100. ATR = 4.0. Stop = 100 - (1.5 * 4) = 94. Target = 100 + (2.0 * 6) = 112.
    # Risk distance = 6.0. 1.0R trigger = 106.0.
    # Bar 36: opens at 100. High reaches 115 (+1.5R!), but low drops to 85 (breaching 94)!
    # On bar 36, stop active is 94. Because low <= 94, it MUST exit on initial stop = 94.
    df = _make_daily_frame(dates, closes, highs=highs, lows=lows, opens=opens)
    candles = {"TEST": df}

    def eval_fn(symbol, frame):
        if len(frame) == 36:
            return {"direction": "BUY"}
        return None

    cfg = {
        "backtest": {
            "atr_period": 14,
            "stop_atr_multiple": 1.5,
            "target_r_multiple": 2.5,
            "fee_bps_per_side": 0.0,
            "slippage_bps_per_side": 0.0,
            "trailing_stop_mode": "breakeven_1r",
        }
    }
    res = simulate_period(candles, eval_fn, start_date=dates[0], end_date=dates[-1], config=cfg)
    assert len(res["trades"]) == 1
    t = res["trades"][0]
    # Trade was stopped out on bar 36 before ratcheting
    assert t["exit_reason"] == "stop"
    assert t["exit_price"] == 94.0


def test_trailing_stop_breakeven_1r_ratchets_on_next_bar():
    """Verify bar hitting +1.0R stays open and ratchets stop to entry starting on next bar."""
    dates = [date(2026, 6, 1) + timedelta(days=i) for i in range(40)]
    
    # Warmup 35 bars (0..34), index 35 is signal bar, index 36 is entry bar, index 37 is next bar
    opens = [100.0] * 40
    highs = [102.0] * 40
    lows = [98.0] * 40
    closes = [100.0] * 40

    # Entry bar 36: open=100.0, reaches 108.0 (+1.3R), low=98.0 (above stop 94.0), close=107.0
    opens[36] = 100.0
    highs[36] = 108.0
    lows[36] = 98.0
    closes[36] = 107.0

    # Next bar 37: open=107.0, low=98.0 (drops below ratcheted stop of 100.0)
    opens[37] = 107.0
    highs[37] = 107.0
    lows[37] = 98.0
    closes[37] = 98.0
    
    df = _make_daily_frame(dates, closes, highs=highs, lows=lows, opens=opens)
    candles = {"TEST": df}

    def eval_fn(symbol, frame):
        if len(frame) == 36:
            return {"direction": "BUY"}
        return None

    cfg = {
        "backtest": {
            "atr_period": 14,
            "stop_atr_multiple": 1.5,
            "target_r_multiple": 2.5,
            "fee_bps_per_side": 0.0,
            "slippage_bps_per_side": 0.0,
            "trailing_stop_mode": "breakeven_1r",
        }
    }
    res = simulate_period(candles, eval_fn, start_date=dates[0], end_date=dates[-1], config=cfg)
    assert len(res["trades"]) == 1
    t = res["trades"][0]
    assert t["exit_reason"] == "trailing_stop"
    assert t["exit_price"] == 100.0
    assert t["net_return_pct"] == 0.0


def test_trailing_stop_atr_trail():
    """Verify atr_trail ratchets stop using close - trail_atr_multiple * ATR once 1R is reached."""
    dates = [date(2026, 6, 1) + timedelta(days=i) for i in range(40)]
    
    opens = [100.0] * 40
    highs = [102.0] * 40
    lows = [98.0] * 40
    closes = [100.0] * 40

    # Entry bar 36: entry at 100.0, reaches 108.0 (+1.3R), close 106.0
    # Ratcheted stop = 106.0 - 2.0 * 4.0 = 98.0 (above initial stop 94.0)
    opens[36] = 100.0
    highs[36] = 108.0
    lows[36] = 98.0
    closes[36] = 106.0

    # Next bar 37: opens 106.0, low drops to 97.0 <= 98.0 -> exits on trailing_stop at 98.0
    opens[37] = 106.0
    highs[37] = 106.0
    lows[37] = 97.0
    closes[37] = 97.0
    
    df = _make_daily_frame(dates, closes, highs=highs, lows=lows, opens=opens)
    candles = {"TEST": df}

    def eval_fn(symbol, frame):
        if len(frame) == 36:
            return {"direction": "BUY"}
        return None

    cfg = {
        "backtest": {
            "atr_period": 14,
            "stop_atr_multiple": 1.5,
            "target_r_multiple": 3.0,
            "fee_bps_per_side": 0.0,
            "slippage_bps_per_side": 0.0,
            "trailing_stop_mode": "atr_trail",
            "trail_atr_multiple": 2.0,
        }
    }
    res = simulate_period(candles, eval_fn, start_date=dates[0], end_date=dates[-1], config=cfg)
    assert len(res["trades"]) == 1
    t = res["trades"][0]
    assert t["exit_reason"] == "trailing_stop"
    assert t["exit_price"] == 98.0
