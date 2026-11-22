"""
Tests for Average Directional Index (ADX) calculation and guardrail filter.
"""
import numpy as np
import pandas as pd
import pytest

from src.signals.adx import compute_adx, adx_filter
from src.db import Stock
from src.scanner import Scanner


def _make_trending_up_candles(n: int = 60, start_price: float = 100.0) -> pd.DataFrame:
    """Generate steadily climbing candles with minimal retracement (strong trend)."""
    rows = []
    price = start_price
    for i in range(n):
        price += 2.0
        rows.append({
            "open": price - 1.0,
            "high": price + 2.0,
            "low": price - 1.5,
            "close": price + 1.0,
            "volume": 100000 + i * 1000,
        })
    return pd.DataFrame(rows)


def _make_trending_down_candles(n: int = 60, start_price: float = 300.0) -> pd.DataFrame:
    """Generate steadily falling candles with minimal retracement (strong downtrend)."""
    rows = []
    price = start_price
    for i in range(n):
        price -= 2.0
        rows.append({
            "open": price + 1.0,
            "high": price + 1.5,
            "low": price - 2.0,
            "close": price - 1.0,
            "volume": 100000 + i * 1000,
        })
    return pd.DataFrame(rows)


def _make_choppy_sideways_candles(n: int = 60, base_price: float = 100.0) -> pd.DataFrame:
    """Generate oscillating candles that bounce up and down in a narrow range (choppy/no trend)."""
    rows = []
    for i in range(n):
        # Oscillate between 98 and 102
        offset = 2.0 if i % 2 == 0 else -2.0
        center = base_price + offset
        rows.append({
            "open": center - 0.5,
            "high": center + 1.5,
            "low": center - 1.5,
            "close": center + 0.5,
            "volume": 50000,
        })
    return pd.DataFrame(rows)


class TestADXComputation:
    def test_compute_adx_uptrend(self):
        df = _make_trending_up_candles(70)
        res = compute_adx(df, period=14)
        assert len(res) == 70
        assert "adx" in res.columns
        assert "plus_di" in res.columns
        assert "minus_di" in res.columns

        # In strong uptrend: +DI should exceed -DI and ADX should be high (> 25)
        latest_adx = res["adx"].iloc[-1]
        latest_plus = res["plus_di"].iloc[-1]
        latest_minus = res["minus_di"].iloc[-1]
        assert latest_plus > latest_minus
        assert latest_adx > 25.0

    def test_compute_adx_downtrend(self):
        df = _make_trending_down_candles(70)
        res = compute_adx(df, period=14)
        latest_adx = res["adx"].iloc[-1]
        latest_plus = res["plus_di"].iloc[-1]
        latest_minus = res["minus_di"].iloc[-1]
        assert latest_minus > latest_plus
        assert latest_adx > 25.0

    def test_compute_adx_choppy_oscillating(self):
        df = _make_choppy_sideways_candles(70)
        res = compute_adx(df, period=14)
        latest_adx = res["adx"].iloc[-1]
        # In a tight oscillating market, ADX should be low
        assert latest_adx < 20.0


class TestADXFilter:
    def test_adx_filter_passed_strong(self):
        df = _make_trending_up_candles(70)
        result = adx_filter(df, period=14, min_adx=20.0, strong_threshold=25.0)
        assert result["passed"] is True
        assert result["trend_strength"] == "strong"
        assert result["adx"] >= 25.0
        assert result["reason"] == "ok"

    def test_adx_filter_rejected_weak_choppy(self):
        df = _make_choppy_sideways_candles(70)
        result = adx_filter(df, period=14, min_adx=20.0, strong_threshold=25.0)
        assert result["passed"] is False
        assert result["trend_strength"] == "weak"
        assert result["adx"] < 20.0
        assert result["reason"] == "adx_below_threshold"

    def test_adx_filter_insufficient_data(self):
        short_df = _make_trending_up_candles(10)
        result = adx_filter(short_df, period=14)
        assert result["passed"] is True
        assert result["trend_strength"] == "unavailable"
        assert result["reason"] == "insufficient_history"


class TestScannerADXGuardrailIntegration:
    def test_scanner_rejects_choppy_market_when_adx_filter_enabled(self, monkeypatch):
        """When confluence passes but ADX is low, scan must reject signal."""
        scanner = Scanner.__new__(Scanner)
        scanner.cfg = {
            "guardrails": {
                "wick_filter": {"enabled": False},
                "adx_filter": {
                    "enabled": True,
                    "period": 14,
                    "min_adx": 25.0,
                    "strong_trend_threshold": 30.0,
                    "apply_to_provisional": True,
                    "apply_to_daily": True,
                },
            },
            "confluence": {"mode": "crossover", "min_groups_to_alert": 1, "direction_consensus": True},
            "signals": {
                "rsi": {"enabled": False},
                "macd": {"enabled": False},
                "volume_breakout": {"enabled": False},
                "ema_crossover": {"enabled": False},
            },
        }
        scanner.guardrails_cfg = scanner.cfg["guardrails"]
        scanner.confluence_cfg = scanner.cfg["confluence"]
        scanner.signal_cfg = scanner.cfg["signals"]
        scanner.universe_cfg = {"min_price": 10}

        # Mock confluence returning a BUY signal
        import src.scanner as scanner_module
        monkeypatch.setattr(
            scanner_module,
            "evaluate_confluence",
            lambda checks, **kwargs: {
                "direction": "BUY",
                "score": 2,
                "total_groups": 3,
                "group_votes": {"trend": "BUY", "momentum": "BUY"},
                "conflicting_groups": [],
                "checks": [],
            },
        )

        stock = Stock(symbol="CHOPPY", company_name="Choppy Co")
        choppy_df = _make_choppy_sideways_candles(60)

        res, _ = scanner._scan_stock(stock, choppy_df, provisional=False)
        assert res is None, "Should be rejected by ADX filter"

    def test_scanner_accepts_trending_market_and_enriches_adx(self, monkeypatch):
        """When confluence passes and ADX >= min_adx, scan accepts signal and records adx."""
        scanner = Scanner.__new__(Scanner)
        scanner.cfg = {
            "guardrails": {
                "wick_filter": {"enabled": False},
                "adx_filter": {
                    "enabled": True,
                    "period": 14,
                    "min_adx": 20.0,
                    "strong_trend_threshold": 25.0,
                    "apply_to_provisional": True,
                    "apply_to_daily": True,
                },
            },
            "confluence": {"mode": "crossover", "min_groups_to_alert": 1, "direction_consensus": True},
            "signals": {
                "rsi": {"enabled": False},
                "macd": {"enabled": False},
                "volume_breakout": {"enabled": False},
                "ema_crossover": {"enabled": False},
            },
        }
        scanner.guardrails_cfg = scanner.cfg["guardrails"]
        scanner.confluence_cfg = scanner.cfg["confluence"]
        scanner.signal_cfg = scanner.cfg["signals"]
        scanner.universe_cfg = {"min_price": 10}

        import src.scanner as scanner_module
        monkeypatch.setattr(
            scanner_module,
            "evaluate_confluence",
            lambda checks, **kwargs: {
                "direction": "BUY",
                "score": 2,
                "total_groups": 3,
                "group_votes": {"trend": "BUY"},
                "conflicting_groups": [],
                "checks": [],
            },
        )

        stock = Stock(symbol="TRENDING", company_name="Trending Co")
        trending_df = _make_trending_up_candles(70)

        res, _ = scanner._scan_stock(stock, trending_df, provisional=False)
        assert res is not None
        assert res["direction"] == "BUY"
        assert "adx" in res
        assert res["adx"]["passed"] is True
        assert res["adx"]["trend_strength"] == "strong"
        assert res["adx"]["adx"] >= 25.0
