"""Deterministic, informational benchmark/sector context helpers."""
from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd


def regime_for_candles(frame: pd.DataFrame, *, short_window: int = 20, long_window: int = 50) -> dict[str, Any]:
    """Classify the latest completed index close against its moving averages."""
    if frame is None or frame.empty or len(frame) < long_window:
        return {"status": "unavailable", "reason": "insufficient_index_history"}
    closes = pd.to_numeric(frame["close"], errors="coerce")
    if closes.isna().any() or (closes <= 0).any():
        return {"status": "unavailable", "reason": "invalid_index_history"}
    close = float(closes.iloc[-1])
    short_sma = float(closes.tail(short_window).mean())
    long_sma = float(closes.tail(long_window).mean())
    above_short, above_long = close >= short_sma, close >= long_sma
    label = "bullish" if above_short and above_long else "bearish" if not above_short and not above_long else "mixed"
    raw_date = pd.Timestamp(frame["ts"].iloc[-1]).date()
    return {
        "status": "available", "regime": label, "close": close,
        "short_sma": short_sma, "long_sma": long_sma,
        "session_date": raw_date.isoformat(),
    }


def relative_strength(stock: pd.DataFrame, index: pd.DataFrame, *, lookback: int = 20) -> dict[str, Any]:
    """Return stock minus index percentage return over common completed sessions."""
    if stock is None or index is None or stock.empty or index.empty:
        return {"status": "unavailable", "reason": "missing_history"}
    left = stock[["ts", "close"]].copy()
    right = index[["ts", "close"]].copy()
    left["session"] = pd.to_datetime(left["ts"], errors="coerce").dt.date
    right["session"] = pd.to_datetime(right["ts"], errors="coerce").dt.date
    common = left.merge(right, on="session", suffixes=("_stock", "_index")).dropna()
    if len(common) < lookback + 1:
        return {"status": "unavailable", "reason": "insufficient_common_sessions"}
    common = common.tail(lookback + 1)
    stock_start, stock_end = float(common["close_stock"].iloc[0]), float(common["close_stock"].iloc[-1])
    index_start, index_end = float(common["close_index"].iloc[0]), float(common["close_index"].iloc[-1])
    if min(stock_start, stock_end, index_start, index_end) <= 0:
        return {"status": "unavailable", "reason": "invalid_close"}
    stock_return = (stock_end / stock_start - 1.0) * 100.0
    index_return = (index_end / index_start - 1.0) * 100.0
    return {
        "status": "available", "lookback_sessions": lookback,
        "stock_return_pct": stock_return, "index_return_pct": index_return,
        "relative_strength_pct": stock_return - index_return,
        "as_of": common["session"].iloc[-1].isoformat(),
    }


def average_traded_value(
    frame: pd.DataFrame, *, sessions: int = 20, minimum_observations: int | None = None
) -> dict[str, Any]:
    """Compute mean close×volume in INR using completed daily bars only."""
    minimum_observations = minimum_observations or sessions
    if frame is None or frame.empty or len(frame) < minimum_observations:
        return {"status": "unavailable", "reason": "insufficient_sessions", "observations": 0 if frame is None else len(frame)}
    window = frame.tail(sessions)
    closes = pd.to_numeric(window["close"], errors="coerce")
    volumes = pd.to_numeric(window["volume"], errors="coerce")
    turnover = closes * volumes
    if turnover.isna().any() or (turnover < 0).any():
        return {"status": "unavailable", "reason": "invalid_volume_history", "observations": len(window)}
    return {
        "status": "available", "average_inr": float(turnover.mean()),
        "observations": len(window), "lookback_sessions": sessions,
        "as_of": pd.Timestamp(window["ts"].iloc[-1]).date().isoformat(),
    }
