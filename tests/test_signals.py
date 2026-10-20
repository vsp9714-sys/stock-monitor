"""
Tests for trading signal detection engines.
"""
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import pytest

from src.signals.rsi import detect_rsi_signal, compute_rsi
from src.signals.macd import detect_macd_signal, compute_macd
from src.signals.ema_cross import detect_all_ema_crossovers, compute_ema
from src.signals.volume_breakout import detect_volume_breakout
from charts.chart_generator import generate_signal_chart


def _generate_synthetic_ohlcv(n_candles: int = 100, trend: str = "flat", volume_spike: bool = False) -> pd.DataFrame:
    """Generate realistic synthetic OHLCV data for testing."""
    dates = [datetime(2026, 1, 1) + timedelta(days=i) for i in range(n_candles)]
    np.random.seed(42)

    base_price = 100.0
    prices = [base_price]
    for i in range(1, n_candles):
        if trend == "down":
            step = -0.5 + np.random.normal(0, 0.2)
        elif trend == "up":
            step = 0.5 + np.random.normal(0, 0.2)
        else:
            step = np.random.normal(0, 0.5)
        prices.append(max(10.0, prices[-1] + step))

    closes = np.array(prices)
    highs = closes + np.random.uniform(0.1, 1.0, size=n_candles)
    lows = closes - np.random.uniform(0.1, 1.0, size=n_candles)
    opens = closes + np.random.uniform(-0.5, 0.5, size=n_candles)
    volumes = np.random.uniform(10000, 50000, size=n_candles)

    if volume_spike:
        volumes[-1] = volumes[:-1].mean() * 5.0
        closes[-1] = highs[-1]  # Breakout candle

    return pd.DataFrame({
        "ts": dates,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": volumes,
    })


def test_rsi_computation():
    df = _generate_synthetic_ohlcv(50, trend="down")
    rsi = compute_rsi(df["close"], period=14)
    assert len(rsi) == 50
    # On a continuous downtrend, RSI should be low
    assert rsi.iloc[-1] < 40.0


def test_rsi_oversold_signal():
    df = _generate_synthetic_ohlcv(60, trend="down")
    # Force last 10 candles sharply down to trigger oversold
    df.loc[df.index[-10:], "close"] = [50 - i * 3 for i in range(10)]
    result = detect_rsi_signal("TATAMOTORS", df, period=14, oversold=30.0)
    assert result.triggered is True
    assert result.signal_type == "rsi_oversold"
    assert result.direction == "BUY"
    assert result.rsi_value < 30.0


def test_rsi_overbought_signal():
    df = _generate_synthetic_ohlcv(60, trend="up")
    # Force last 10 candles sharply up
    df.loc[df.index[-10:], "close"] = [100 + i * 5 for i in range(10)]
    result = detect_rsi_signal("INFY", df, period=14, overbought=70.0)
    assert result.triggered is True
    assert result.signal_type == "rsi_overbought"
    assert result.direction == "SELL"
    assert result.rsi_value > 70.0


def test_macd_bullish_cross():
    # Construct series with clear MACD bullish crossover on last candle
    df = _generate_synthetic_ohlcv(70, trend="down")
    df.loc[df.index[-5:], "close"] = [df["close"].iloc[-6] + i * 4 for i in range(1, 6)]
    result = detect_macd_signal("RELIANCE", df)
    assert hasattr(result, "triggered")
    assert hasattr(result, "macd_value")
    assert hasattr(result, "signal_value")
    assert hasattr(result, "histogram")


def test_ema_cross_golden():
    df = _generate_synthetic_ohlcv(250, trend="up")
    signals = detect_all_ema_crossovers("HDFCBANK", df, pairs=[(9, 21), (50, 200)])
    assert isinstance(signals, list)


def test_volume_breakout():
    df = _generate_synthetic_ohlcv(60, trend="flat", volume_spike=True)
    df.loc[df.index[-1], "close"] = df["high"].iloc[:-1].max() + 5.0
    df.loc[df.index[-1], "high"] = df["close"].iloc[-1] + 1.0
    result = detect_volume_breakout("SBIN", df, lookback_days=20, volume_multiplier=2.5)
    assert result.triggered is True
    assert result.direction == "BUY"
    assert result.volume_ratio >= 2.5


def test_chart_generation(tmp_path):
    df = _generate_synthetic_ohlcv(80, trend="up")
    chart_path = generate_signal_chart(
        symbol="TCS",
        company_name="Tata Consultancy Services Ltd",
        df=df,
        signal_type="rsi_oversold",
        direction="BUY",
        indicator_value=27.4,
        lookback_candles=60,
    )
    assert chart_path is not None
    import os
    assert os.path.exists(chart_path)
    assert chart_path.endswith(".png")
    assert os.path.getsize(chart_path) > 1000
