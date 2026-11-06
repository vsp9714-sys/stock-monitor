"""
Signal engine — RSI (Relative Strength Index).

Detects:
  - RSI < oversold_threshold  → BUY signal (rsi_oversold)
  - RSI > overbought_threshold → SELL signal (rsi_overbought)

Uses Wilder's smoothing (standard RSI formula).
"""
import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class RSIResult:
    symbol: str
    signal_type: Optional[str]    # "rsi_oversold", "rsi_overbought", or None
    direction: Optional[str]       # "BUY", "SELL", or None
    rsi_value: float
    price: float
    triggered: bool
    mode: str = "mean_reversion"
    trigger_reason: Optional[str] = None
    event_triggered: bool = False
    event_direction: Optional[str] = None
    trend_direction: Optional[str] = None


def compute_rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    """
    Compute RSI using Wilder's EMA smoothing method.
    Returns a Series of RSI values (0-100).
    """
    delta = closes.diff()
    gains = delta.clip(lower=0)
    losses = (-delta).clip(lower=0)

    # Wilder's smoothing (alpha = 1/period)
    avg_gain = gains.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = losses.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def detect_rsi_signal(
    symbol: str,
    df: pd.DataFrame,
    period: int = 14,
    oversold: float = 30.0,
    overbought: float = 70.0,
    mode: str = "mean_reversion",
    confirmation: str = "level",
    bullish_threshold: float = 55.0,
    bearish_threshold: float = 45.0,
) -> RSIResult:
    """
    Detect RSI signals for a stock.

    Args:
        symbol: Stock symbol (for logging/result)
        df: DataFrame with 'close' column (daily or intraday candles)
        period: RSI period (default 14)
        oversold: Buy threshold (default 30)
        overbought: Sell threshold for mean-reversion (default 70)
        mode: ``mean_reversion`` (legacy extreme-level rule) or
            ``trend_momentum`` (trend-aligned RSI thresholds).
        confirmation: Mean-reversion trigger policy: ``level`` preserves the
            previous behavior; ``cross_back`` waits for RSI to re-enter the
            range after reaching an extreme.
        bullish_threshold / bearish_threshold: trend-momentum RSI levels.

    Returns:
        RSIResult with signal details.
    """
    if mode not in {"mean_reversion", "trend_momentum"}:
        raise ValueError(f"Unsupported RSI mode: {mode}")
    if confirmation not in {"level", "cross_back"}:
        raise ValueError(f"Unsupported RSI confirmation: {confirmation}")

    min_candles = period + 10
    if len(df) < min_candles:
        logger.debug(f"{symbol}: Not enough candles for RSI ({len(df)} < {min_candles})")
        return RSIResult(
            symbol=symbol, signal_type=None, direction=None,
            rsi_value=float("nan"), price=float("nan"), triggered=False, mode=mode
        )

    closes = df["close"].astype(float)
    rsi_series = compute_rsi(closes, period)
    current_rsi = rsi_series.iloc[-1]
    previous_rsi = rsi_series.iloc[-2]
    current_price = closes.iloc[-1]

    if np.isnan(current_rsi):
        return RSIResult(
            symbol=symbol, signal_type=None, direction=None,
            rsi_value=float("nan"), price=current_price, triggered=False, mode=mode
        )

    if np.isnan(previous_rsi):
        previous_rsi = current_rsi

    if mode == "mean_reversion":
        direction = "BUY" if current_rsi <= oversold else (
            "SELL" if current_rsi >= overbought else None
        )
        event_direction = (
            "BUY" if current_rsi <= oversold and previous_rsi > oversold else
            "SELL" if current_rsi >= overbought and previous_rsi < overbought else None
        )
        if confirmation == "cross_back":
            direction = (
                "BUY" if previous_rsi < oversold and current_rsi >= oversold else
                "SELL" if previous_rsi > overbought and current_rsi <= overbought else None
            )
            event_direction = direction
        signal_type = "rsi_oversold" if direction == "BUY" else "rsi_overbought"
        reason = (
            f"Mean reversion {confirmation}: RSI {current_rsi:.2f}" if direction else None
        )
    else:
        direction = "BUY" if current_rsi >= bullish_threshold else (
            "SELL" if current_rsi <= bearish_threshold else None
        )
        event_direction = (
            "BUY" if current_rsi >= bullish_threshold and previous_rsi < bullish_threshold else
            "SELL" if current_rsi <= bearish_threshold and previous_rsi > bearish_threshold else None
        )
        signal_type = "rsi_trend_momentum"
        reason = (
            f"Trend momentum {direction}: RSI {current_rsi:.2f}" if direction else None
        )

    triggered = direction is not None
    return RSIResult(
        symbol=symbol,
        signal_type=signal_type if triggered else None,
        direction=direction,
        rsi_value=round(current_rsi, 2),
        price=current_price,
        triggered=triggered,
        mode=mode,
        trigger_reason=reason,
        event_triggered=event_direction is not None,
        event_direction=event_direction,
        trend_direction=(
            "BUY" if current_rsi >= bullish_threshold else
            "SELL" if current_rsi <= bearish_threshold else None
        ),
    )
