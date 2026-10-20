"""
Signal engine — Volume Spike + Price Breakout.

Detects:
  - Volume spike: current volume ≥ N× 20-day average volume
  - Price breakout: current close breaks above/below N-day high/low

Signal types:
  - volume_breakout_bullish → high volume + price breaks above recent high → BUY
  - volume_breakout_bearish → high volume + price breaks below recent low → SELL
  - volume_spike → high volume but no clear price breakout (informational)

This pattern is a strong swing trading confirmation signal:
it shows that the move is backed by conviction (institutional interest).
"""
import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class VolumeBreakoutResult:
    symbol: str
    signal_type: Optional[str]    # "volume_breakout_bullish", "volume_breakout_bearish", "volume_spike"
    direction: Optional[str]       # "BUY", "SELL", "NEUTRAL", or None
    current_volume: float
    avg_volume: float
    volume_ratio: float
    current_price: float
    breakout_level: Optional[float]  # High/low that was breached
    triggered: bool


def detect_volume_breakout(
    symbol: str,
    df: pd.DataFrame,
    volume_multiplier: float = 2.0,
    lookback_days: int = 20,
) -> VolumeBreakoutResult:
    """
    Detect volume spike + price breakout signals.

    Logic:
    1. Compute 20-day average volume (excluding today)
    2. Check if today's volume ≥ volume_multiplier × avg_volume
    3. If yes:
       - If price closes above 20-day high → bullish breakout (BUY)
       - If price closes below 20-day low → bearish breakout (SELL)
       - Otherwise → plain volume spike (NEUTRAL, informational)

    Args:
        symbol: Stock symbol
        df: DataFrame with 'open','high','low','close','volume' columns (daily)
        volume_multiplier: Minimum ratio of current to avg volume
        lookback_days: Days to compute avg volume and price high/low

    Returns:
        VolumeBreakoutResult.
    """
    min_candles = lookback_days + 5
    if len(df) < min_candles:
        logger.debug(
            f"{symbol}: Not enough candles for volume breakout "
            f"({len(df)} < {min_candles})"
        )
        return VolumeBreakoutResult(
            symbol=symbol, signal_type=None, direction=None,
            current_volume=float("nan"), avg_volume=float("nan"),
            volume_ratio=float("nan"), current_price=float("nan"),
            breakout_level=None, triggered=False,
        )

    # Use last N+1 candles: [0..N-1] = reference period, [N] = current
    window = df.tail(lookback_days + 1).reset_index(drop=True)
    ref = window.iloc[:-1]   # Reference period (excluding today)
    current = window.iloc[-1]  # Today

    avg_volume = ref["volume"].mean()
    current_volume = float(current["volume"])
    volume_ratio = current_volume / avg_volume if avg_volume > 0 else 0

    current_price = float(current["close"])
    ref_high = float(ref["high"].max())
    ref_low = float(ref["low"].min())

    if volume_ratio < volume_multiplier:
        # Not a volume spike
        return VolumeBreakoutResult(
            symbol=symbol, signal_type=None, direction=None,
            current_volume=current_volume, avg_volume=avg_volume,
            volume_ratio=round(volume_ratio, 2), current_price=current_price,
            breakout_level=None, triggered=False,
        )

    # Volume spike confirmed — check price breakout direction
    if current_price > ref_high:
        return VolumeBreakoutResult(
            symbol=symbol,
            signal_type="volume_breakout_bullish",
            direction="BUY",
            current_volume=current_volume,
            avg_volume=avg_volume,
            volume_ratio=round(volume_ratio, 2),
            current_price=current_price,
            breakout_level=ref_high,
            triggered=True,
        )

    if current_price < ref_low:
        return VolumeBreakoutResult(
            symbol=symbol,
            signal_type="volume_breakout_bearish",
            direction="SELL",
            current_volume=current_volume,
            avg_volume=avg_volume,
            volume_ratio=round(volume_ratio, 2),
            current_price=current_price,
            breakout_level=ref_low,
            triggered=True,
        )

    # High volume but no breakout (inside range)
    return VolumeBreakoutResult(
        symbol=symbol,
        signal_type="volume_spike",
        direction="NEUTRAL",
        current_volume=current_volume,
        avg_volume=avg_volume,
        volume_ratio=round(volume_ratio, 2),
        current_price=current_price,
        breakout_level=None,
        triggered=True,  # Still report as triggered for informational alert
    )
