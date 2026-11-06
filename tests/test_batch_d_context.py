from datetime import date

import pandas as pd

from src.derivatives import nearest_expiry, summarize_option_chain
from src.market_context import average_traded_value, regime_for_candles, relative_strength


def _bars(closes, volumes=None, start="2026-01-01"):
    volumes = volumes or [100] * len(closes)
    return pd.DataFrame({
        "ts": pd.date_range(start, periods=len(closes), freq="D"),
        "open": closes,
        "high": [value + 1 for value in closes],
        "low": [value - 1 for value in closes],
        "close": closes,
        "volume": volumes,
    })


def test_market_regime_requires_history_and_uses_completed_closes():
    assert regime_for_candles(_bars([10, 11]), short_window=2, long_window=3)["status"] == "unavailable"
    result = regime_for_candles(_bars([10, 11, 12, 13]), short_window=2, long_window=3)
    assert result["status"] == "available"
    assert result["regime"] == "bullish"


def test_relative_strength_uses_only_common_sessions():
    stock = _bars([100, 105, 110, 120], start="2026-01-01")
    index = _bars([100, 102, 104, 110], start="2026-01-02")
    result = relative_strength(stock, index, lookback=2)
    assert result["status"] == "available"
    assert result["as_of"] == "2026-01-04"
    assert result["relative_strength_pct"] > 0


def test_traded_value_uses_observation_floor_and_close_times_volume():
    frame = _bars([100, 200, 300], [10, 20, 30])
    assert average_traded_value(frame, sessions=3, minimum_observations=4)["reason"] == "insufficient_sessions"
    result = average_traded_value(frame, sessions=3, minimum_observations=3)
    assert result["average_inr"] == (100 * 10 + 200 * 20 + 300 * 30) / 3
    assert result["observations"] == 3


def test_option_context_requires_complete_consistent_chain_and_nearest_expiry():
    contracts = [{"expiry": "2026-11-26"}, {"expiry": "2026-11-12"}, {"expiry": "2026-10-29"}]
    assert nearest_expiry(contracts, date(2026, 11, 6)) == "2026-11-12"
    row = {
        "expiry": "2026-11-12",
        "call_options": {"market_data": {"oi": 100, "prev_oi": 80}},
        "put_options": {"market_data": {"oi": 50, "prev_oi": 40}},
    }
    result = summarize_option_chain([row], expiry_date="2026-11-12")
    assert result["status"] == "available"
    assert result["put_call_oi_ratio"] == 0.5
    assert result["total_oi_change_pct"] == 25
    assert summarize_option_chain([row], expiry_date="2026-11-19")["reason"] == "mixed_expiries"
    assert summarize_option_chain([], expiry_date="2026-11-12")["reason"] == "empty_option_chain"
