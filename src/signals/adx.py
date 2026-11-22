"""
Signal engine — Average Directional Index (ADX) & Trend Strength Filter.

Wilder's ADX quantifies trend strength independently of direction.
- ADX >= 25: Strong trend (high conviction breakouts / swings)
- 20 <= ADX < 25: Moderate trend
- ADX < 20: Weak / sideways / choppy market (high risk of false breakout whipsaws)
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def compute_adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """
    Compute Wilder's +DI, -DI, and ADX series.

    Args:
        df: DataFrame with 'high', 'low', 'close' columns.
        period: Smoothing period (standard Wilder's default is 14).

    Returns:
        DataFrame with columns ['plus_di', 'minus_di', 'adx'].
    """
    if df is None or len(df) < (2 * period) + 1:
        empty = pd.Series(index=df.index if df is not None else None, dtype=float)
        return pd.DataFrame({"plus_di": empty, "minus_di": empty, "adx": empty})

    required_cols = {"high", "low", "close"}
    if not required_cols.issubset(df.columns):
        raise ValueError(f"DataFrame must contain {required_cols}")

    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    prev_close = close.shift(1)

    # True Range (TR)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    # Directional Movement (+DM, -DM)
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    up_move = high - prev_high
    down_move = prev_low - low

    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
        index=df.index,
        dtype=float,
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
        index=df.index,
        dtype=float,
    )

    # Wilder's exponential smoothing (alpha = 1 / period)
    alpha = 1.0 / period
    tr_smooth = tr.ewm(alpha=alpha, min_periods=period, adjust=False).mean()
    plus_dm_smooth = plus_dm.ewm(alpha=alpha, min_periods=period, adjust=False).mean()
    minus_dm_smooth = minus_dm.ewm(alpha=alpha, min_periods=period, adjust=False).mean()

    # Directional Indicators (+DI, -DI)
    plus_di = 100.0 * (plus_dm_smooth / tr_smooth.replace(0, np.nan))
    minus_di = 100.0 * (minus_dm_smooth / tr_smooth.replace(0, np.nan))

    # Directional Index (DX) and ADX
    di_sum = plus_di + minus_di
    dx = 100.0 * ((plus_di - minus_di).abs() / di_sum.replace(0, np.nan))
    adx = dx.ewm(alpha=alpha, min_periods=period, adjust=False).mean()

    return pd.DataFrame({
        "plus_di": plus_di,
        "minus_di": minus_di,
        "adx": adx,
    }, index=df.index)


def adx_filter(
    df: pd.DataFrame,
    period: int = 14,
    min_adx: float = 20.0,
    strong_threshold: float = 25.0,
    direction: Optional[str] = None,
    require_di_alignment: bool = True,
) -> dict[str, Any]:
    """
    Evaluate ADX trend strength against configured thresholds.

    Returns:
        dict with:
        - passed (bool): True if trend is strong enough (adx >= min_adx)
        - adx (float | None): Latest ADX value
        - plus_di (float | None): Latest +DI value
        - minus_di (float | None): Latest -DI value
        - trend_strength (str): 'strong', 'moderate', 'weak', or 'unavailable'
        - reason (str): Explanatory reason
    """
    if df is None or len(df) < (2 * period) + 1:
        return {
            "passed": True,
            "adx": None,
            "plus_di": None,
            "minus_di": None,
            "trend_strength": "unavailable",
            "reason": "insufficient_history",
        }

    try:
        adx_df = compute_adx(df, period=period)
        latest_adx = adx_df["adx"].iloc[-1]
        latest_plus_di = adx_df["plus_di"].iloc[-1]
        latest_minus_di = adx_df["minus_di"].iloc[-1]

        if np.isnan(latest_adx):
            return {
                "passed": True,
                "adx": None,
                "plus_di": None,
                "minus_di": None,
                "trend_strength": "unavailable",
                "reason": "adx_nan",
            }

        adx_val = float(latest_adx)
        p_di = float(latest_plus_di) if not np.isnan(latest_plus_di) else 0.0
        m_di = float(latest_minus_di) if not np.isnan(latest_minus_di) else 0.0

        passed = adx_val >= min_adx
        reason = "ok" if passed else "adx_below_threshold"

        if passed and require_di_alignment and direction:
            dir_upper = str(direction).upper()
            if dir_upper == "BUY" and p_di <= m_di:
                passed = False
                reason = "di_opposes_buy"
            elif dir_upper == "SELL" and m_di <= p_di:
                passed = False
                reason = "di_opposes_sell"

        if adx_val >= strong_threshold:
            strength = "strong"
        elif adx_val >= min_adx:
            strength = "moderate"
        else:
            strength = "weak"

        return {
            "passed": bool(passed),
            "adx": round(adx_val, 2),
            "plus_di": round(p_di, 2),
            "minus_di": round(m_di, 2),
            "trend_strength": strength,
            "reason": reason,
        }
    except Exception as exc:
        logger.warning("Error evaluating ADX filter: %s", exc)
        return {
            "passed": True,
            "adx": None,
            "plus_di": None,
            "minus_di": None,
            "trend_strength": "unavailable",
            "reason": f"error: {exc}",
        }
