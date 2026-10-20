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
) -> RSIResult:
    """
    Detect RSI signals for a stock.

    Args:
        symbol: Stock symbol (for logging/result)
        df: DataFrame with 'close' column (daily or intraday candles)
        period: RSI period (default 14)
        oversold: Buy threshold (default 30)
        overbought: Sell threshold (default 70)

    Returns:
        RSIResult with signal details.
    """
    min_candles = period + 10
    if len(df) < min_candles:
        logger.debug(f"{symbol}: Not enough candles for RSI ({len(df)} < {min_candles})")
        return RSIResult(
            symbol=symbol, signal_type=None, direction=None,
            rsi_value=float("nan"), price=float("nan"), triggered=False
        )

    closes = df["close"].astype(float)
    rsi_series = compute_rsi(closes, period)
    current_rsi = rsi_series.iloc[-1]
    current_price = closes.iloc[-1]

    if np.isnan(current_rsi):
        return RSIResult(
            symbol=symbol, signal_type=None, direction=None,
            rsi_value=float("nan"), price=current_price, triggered=False
        )

    if current_rsi <= oversold:
        return RSIResult(
            symbol=symbol,
            signal_type="rsi_oversold",
            direction="BUY",
            rsi_value=round(current_rsi, 2),
            price=current_price,
            triggered=True,
        )
    if current_rsi >= overbought:
        return RSIResult(
            symbol=symbol,
            signal_type="rsi_overbought",
            direction="SELL",
            rsi_value=round(current_rsi, 2),
            price=current_price,
            triggered=True,
        )

    return RSIResult(
        symbol=symbol, signal_type=None, direction=None,
        rsi_value=round(current_rsi, 2), price=current_price, triggered=False
    )
