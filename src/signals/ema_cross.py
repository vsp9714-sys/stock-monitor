"""
Signal engine — EMA Crossover.

Monitors multiple EMA pairs for crossover events:
  - 9 EMA × 21 EMA  (short-term momentum, swing trading)
  - 21 EMA × 50 EMA (medium-term trend change)
  - 50 EMA × 200 EMA (long-term "Golden Cross" / "Death Cross")

Signal types:
  - ema_{fast}x{slow}_bullish  → fast EMA crosses above slow EMA → BUY
  - ema_{fast}x{slow}_bearish  → fast EMA crosses below slow EMA → SELL
"""
import logging
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class EMACrossResult:
    symbol: str
    signal_type: Optional[str]    # e.g., "ema_9x21_bullish"
    direction: Optional[str]       # "BUY" or "SELL"
    fast_period: int
    slow_period: int
    fast_ema_value: float
    slow_ema_value: float
    price: float
    triggered: bool


def compute_ema(closes: pd.Series, period: int) -> pd.Series:
    """Compute EMA for a given period."""
    return closes.ewm(span=period, adjust=False).mean()


def detect_ema_crossover(
    symbol: str,
    df: pd.DataFrame,
    fast_period: int,
    slow_period: int,
) -> EMACrossResult:
    """
    Detect EMA crossover for a given fast/slow pair.

    Cross is confirmed when the relationship between fast and slow EMA
    changes between the previous and current bar.

    Args:
        symbol: Stock symbol
        df: DataFrame with 'close' column (need at least slow_period + 10 rows)
        fast_period: Short EMA period (e.g., 9)
        slow_period: Long EMA period (e.g., 21)

    Returns:
        EMACrossResult with crossover details.
    """
    min_candles = slow_period + 10
    if len(df) < min_candles:
        logger.debug(
            f"{symbol}: Not enough candles for EMA {fast_period}x{slow_period} "
            f"({len(df)} < {min_candles})"
        )
        return EMACrossResult(
            symbol=symbol, signal_type=None, direction=None,
            fast_period=fast_period, slow_period=slow_period,
            fast_ema_value=float("nan"), slow_ema_value=float("nan"),
            price=float("nan"), triggered=False,
        )

    closes = df["close"].astype(float)
    fast_ema = compute_ema(closes, fast_period)
    slow_ema = compute_ema(closes, slow_period)

    current_price = closes.iloc[-1]
    fast_cur = fast_ema.iloc[-1]
    slow_cur = slow_ema.iloc[-1]
    fast_prev = fast_ema.iloc[-2]
    slow_prev = slow_ema.iloc[-2]

    label = f"ema_{fast_period}x{slow_period}"

    # Bullish crossover: fast crosses above slow
    if fast_prev <= slow_prev and fast_cur > slow_cur:
        cross_label = "Golden Cross" if slow_period == 200 else "Bullish EMA Cross"
        return EMACrossResult(
            symbol=symbol,
            signal_type=f"{label}_bullish",
            direction="BUY",
            fast_period=fast_period,
            slow_period=slow_period,
            fast_ema_value=round(fast_cur, 2),
            slow_ema_value=round(slow_cur, 2),
            price=current_price,
            triggered=True,
        )

    # Bearish crossover: fast crosses below slow
    if fast_prev >= slow_prev and fast_cur < slow_cur:
        cross_label = "Death Cross" if slow_period == 200 else "Bearish EMA Cross"
        return EMACrossResult(
            symbol=symbol,
            signal_type=f"{label}_bearish",
            direction="SELL",
            fast_period=fast_period,
            slow_period=slow_period,
            fast_ema_value=round(fast_cur, 2),
            slow_ema_value=round(slow_cur, 2),
            price=current_price,
            triggered=True,
        )

    return EMACrossResult(
        symbol=symbol, signal_type=None, direction=None,
        fast_period=fast_period, slow_period=slow_period,
        fast_ema_value=round(fast_cur, 2),
        slow_ema_value=round(slow_cur, 2),
        price=current_price, triggered=False,
    )


def detect_all_ema_crossovers(
    symbol: str,
    df: pd.DataFrame,
    pairs: list[tuple[int, int]] = None,
) -> list[EMACrossResult]:
    """
    Detect crossovers for all configured EMA pairs.
    Default pairs: (9,21), (21,50), (50,200)
    Returns only triggered signals.
    """
    if pairs is None:
        pairs = [(9, 21), (21, 50), (50, 200)]

    results = []
    for fast, slow in pairs:
        result = detect_ema_crossover(symbol, df, fast, slow)
        if result.triggered:
            results.append(result)
    return results
