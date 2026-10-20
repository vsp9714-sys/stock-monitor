"""
Signal engine — MACD (Moving Average Convergence/Divergence).

Detects:
  - MACD line crosses ABOVE signal line → BUY (macd_bullish_cross)
  - MACD line crosses BELOW signal line → SELL (macd_bearish_cross)

Standard parameters: fast=12, slow=26, signal=9
"""
import logging
from dataclasses import dataclass
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class MACDResult:
    symbol: str
    signal_type: Optional[str]   # "macd_bullish_cross", "macd_bearish_cross", or None
    direction: Optional[str]      # "BUY", "SELL", or None
    macd_value: float
    signal_value: float
    histogram: float
    price: float
    triggered: bool


def compute_macd(
    closes: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """
    Compute MACD, signal line, and histogram.
    Returns: (macd_line, signal_line, histogram)
    """
    ema_fast = closes.ewm(span=fast, adjust=False).mean()
    ema_slow = closes.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def detect_macd_signal(
    symbol: str,
    df: pd.DataFrame,
    fast: int = 12,
    slow: int = 26,
    signal_period: int = 9,
) -> MACDResult:
    """
    Detect MACD crossover signals.

    A crossover is detected by comparing the last two bars:
    - If macd[−2] < signal[−2] AND macd[−1] > signal[−1] → bullish cross (BUY)
    - If macd[−2] > signal[−2] AND macd[−1] < signal[−1] → bearish cross (SELL)

    Args:
        symbol: Stock symbol
        df: DataFrame with 'close' column
        fast, slow, signal_period: MACD parameters

    Returns:
        MACDResult with crossover details.
    """
    min_candles = slow + signal_period + 5
    if len(df) < min_candles:
        logger.debug(f"{symbol}: Not enough candles for MACD ({len(df)} < {min_candles})")
        return MACDResult(
            symbol=symbol, signal_type=None, direction=None,
            macd_value=float("nan"), signal_value=float("nan"),
            histogram=float("nan"), price=float("nan"), triggered=False,
        )

    closes = df["close"].astype(float)
    macd_line, signal_line, histogram = compute_macd(closes, fast, slow, signal_period)

    current_price = closes.iloc[-1]
    m_cur = macd_line.iloc[-1]
    s_cur = signal_line.iloc[-1]
    m_prev = macd_line.iloc[-2]
    s_prev = signal_line.iloc[-2]

    # Bullish crossover: MACD crosses above signal
    if m_prev <= s_prev and m_cur > s_cur:
        return MACDResult(
            symbol=symbol,
            signal_type="macd_bullish_cross",
            direction="BUY",
            macd_value=round(m_cur, 4),
            signal_value=round(s_cur, 4),
            histogram=round(histogram.iloc[-1], 4),
            price=current_price,
            triggered=True,
        )

    # Bearish crossover: MACD crosses below signal
    if m_prev >= s_prev and m_cur < s_cur:
        return MACDResult(
            symbol=symbol,
            signal_type="macd_bearish_cross",
            direction="SELL",
            macd_value=round(m_cur, 4),
            signal_value=round(s_cur, 4),
            histogram=round(histogram.iloc[-1], 4),
            price=current_price,
            triggered=True,
        )

    return MACDResult(
        symbol=symbol, signal_type=None, direction=None,
        macd_value=round(m_cur, 4),
        signal_value=round(s_cur, 4),
        histogram=round(histogram.iloc[-1], 4),
        price=current_price,
        triggered=False,
    )
