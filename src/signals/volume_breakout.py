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
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")


def nse_expected_volume_fraction(
    current_time: Optional[datetime] = None,
    min_floor: float = 0.05,
) -> float:
    """
    Calculate the expected cumulative volume fraction based on NSE's typical intraday U-curve.

    Session: 09:15 to 15:30 IST (375 minutes total).
    - Phase 1 Opening (09:15 - 10:30, 75 mins): ~30% of total volume (0.40% / min)
    - Phase 2 Midday (10:30 - 14:00, 210 mins): ~40% of total volume (0.19% / min)
    - Phase 3 Power Hour (14:00 - 15:30, 90 mins): ~30% of total volume (0.33% / min)

    Args:
        current_time: Evaluation timestamp (defaults to now).
        min_floor: Lower bound to prevent extreme division in the opening minutes.

    Returns:
        Expected cumulative fraction of daily volume in [min_floor, 1.0].
    """
    if current_time is None:
        return 1.0

    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    ist_time = current_time.astimezone(IST)

    market_open = ist_time.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close = ist_time.replace(hour=15, minute=30, second=0, microsecond=0)

    if ist_time <= market_open:
        return min_floor
    if ist_time >= market_close:
        return 1.0

    elapsed_minutes = (ist_time - market_open).total_seconds() / 60.0
    elapsed_minutes = max(1.0, min(375.0, elapsed_minutes))

    if elapsed_minutes <= 75.0:
        frac = (elapsed_minutes / 75.0) * 0.30
    elif elapsed_minutes <= 285.0:
        frac = 0.30 + ((elapsed_minutes - 75.0) / 210.0) * 0.40
    else:
        frac = 0.70 + ((elapsed_minutes - 285.0) / 90.0) * 0.30

    return float(max(min_floor, min(1.0, frac)))


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
    raw_volume_ratio: Optional[float] = None
    volume_pace: Optional[float] = None
    expected_fraction: Optional[float] = None


def detect_volume_breakout(
    symbol: str,
    df: pd.DataFrame,
    volume_multiplier: float = 2.0,
    lookback_days: int = 20,
    expected_fraction: Optional[float] = None,
    min_fraction_floor: float = 0.05,
) -> VolumeBreakoutResult:
    """
    Detect volume spike + price breakout signals with optional intraday pace normalization.

    Logic:
    1. Compute 20-day average volume (excluding today)
    2. If expected_fraction is provided (< 1.0), evaluate time-weighted volume pace (R-Volume)
       against the expected cumulative fraction of daily volume.
    3. Check if volume ratio/pace ≥ volume_multiplier
    4. If yes:
       - If price closes above 20-day high → bullish breakout (BUY)
       - If price closes below 20-day low → bearish breakout (SELL)
       - Otherwise → plain volume spike (NEUTRAL, informational)

    Args:
        symbol: Stock symbol
        df: DataFrame with 'open','high','low','close','volume' columns (daily)
        volume_multiplier: Minimum ratio of current to avg volume
        lookback_days: Days to compute avg volume and price high/low
        expected_fraction: Expected intraday volume fraction for pace normalization
        min_fraction_floor: Minimum fraction floor to avoid extreme ratios in first minutes

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
            raw_volume_ratio=float("nan"), volume_pace=float("nan"),
            expected_fraction=expected_fraction,
        )

    # Use last N+1 candles: [0..N-1] = reference period, [N] = current
    window = df.tail(lookback_days + 1).reset_index(drop=True)
    ref = window.iloc[:-1]   # Reference period (excluding today)
    current = window.iloc[-1]  # Today

    avg_volume = ref["volume"].mean()
    current_volume = float(current["volume"])
    raw_volume_ratio = current_volume / avg_volume if avg_volume > 0 else 0.0

    if expected_fraction is not None and 0.0 < expected_fraction < 1.0:
        effective_expected = max(expected_fraction, min_fraction_floor) * avg_volume
        volume_ratio = current_volume / effective_expected if effective_expected > 0 else 0.0
        volume_pace = volume_ratio
    else:
        volume_ratio = raw_volume_ratio
        volume_pace = raw_volume_ratio

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
            raw_volume_ratio=round(raw_volume_ratio, 2),
            volume_pace=round(volume_pace, 2),
            expected_fraction=expected_fraction,
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
            raw_volume_ratio=round(raw_volume_ratio, 2),
            volume_pace=round(volume_pace, 2),
            expected_fraction=expected_fraction,
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
            raw_volume_ratio=round(raw_volume_ratio, 2),
            volume_pace=round(volume_pace, 2),
            expected_fraction=expected_fraction,
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
        raw_volume_ratio=round(raw_volume_ratio, 2),
        volume_pace=round(volume_pace, 2),
        expected_fraction=expected_fraction,
    )
