"""Deterministic, informational benchmark/sector context helpers."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

import numpy as np
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


def compute_stock_composite_rs(
    stock: pd.DataFrame,
    benchmark: pd.DataFrame,
    *,
    as_of_date: date | None = None,
    lookback_weights: dict[int, float] | None = None,
    min_common_sessions: int = 21,
) -> dict[str, Any]:
    """Compute weighted multi-horizon excess return vs benchmark using completed bars ts <= as_of_date.

    Default weights: 0.40 * RS(20d) + 0.40 * RS(60d) + 0.20 * RS(120d).
    Falls back gracefully if < 121 sessions are available, requiring at least 21 common sessions.
    """
    if stock is None or benchmark is None or stock.empty or benchmark.empty:
        return {"status": "unavailable", "reason": "missing_history"}

    weights = lookback_weights or {20: 0.40, 60: 0.40, 120: 0.20}

    left = stock[["ts", "close"]].copy()
    right = benchmark[["ts", "close"]].copy()
    left["session"] = pd.to_datetime(left["ts"], errors="coerce").dt.date
    right["session"] = pd.to_datetime(right["ts"], errors="coerce").dt.date

    if as_of_date is not None:
        left = left.loc[left["session"].notna() & (left["session"] <= as_of_date)]
        right = right.loc[right["session"].notna() & (right["session"] <= as_of_date)]

    common = left.merge(right, on="session", suffixes=("_stock", "_index")).dropna()
    common = common.sort_values("session").reset_index(drop=True)

    total_common = len(common)
    if total_common < min_common_sessions:
        return {"status": "unavailable", "reason": "insufficient_common_sessions", "observations": total_common}

    available_lookbacks = [n for n in sorted(weights.keys()) if total_common >= n + 1]
    if not available_lookbacks:
        return {"status": "unavailable", "reason": "insufficient_lookback_history", "observations": total_common}

    weight_sum = sum(float(weights[n]) for n in available_lookbacks)
    if weight_sum <= 0:
        return {"status": "unavailable", "reason": "invalid_weights"}

    stock_end = float(common["close_stock"].iloc[-1])
    index_end = float(common["close_index"].iloc[-1])
    if stock_end <= 0 or index_end <= 0:
        return {"status": "unavailable", "reason": "invalid_close"}

    composite_score = 0.0
    horizon_details = {}
    normalized_weights = {}

    for n in available_lookbacks:
        norm_w = float(weights[n]) / weight_sum
        normalized_weights[n] = norm_w
        stock_start = float(common["close_stock"].iloc[-(n + 1)])
        index_start = float(common["close_index"].iloc[-(n + 1)])
        if stock_start <= 0 or index_start <= 0:
            return {"status": "unavailable", "reason": "invalid_close"}
        stock_ret = (stock_end / stock_start - 1.0) * 100.0
        index_ret = (index_end / index_start - 1.0) * 100.0
        excess = stock_ret - index_ret
        horizon_details[n] = {
            "stock_return_pct": stock_ret,
            "index_return_pct": index_ret,
            "relative_strength_pct": excess,
            "weight": norm_w,
        }
        composite_score += norm_w * excess

    return {
        "status": "available",
        "composite_score": composite_score,
        "observations": total_common,
        "available_lookbacks": available_lookbacks,
        "normalized_weights": normalized_weights,
        "horizons": horizon_details,
        "as_of": common["session"].iloc[-1].isoformat(),
    }


def compute_universe_rs_ratings(
    stocks: dict[str, pd.DataFrame],
    benchmark: pd.DataFrame,
    as_of_date: date,
    lookback_weights: dict[int, float] | None = None,
    min_common_sessions: int = 21,
) -> dict[str, int]:
    """Compute point-in-time cross-sectional RS rating (integer 1-99) vs benchmark.

    Ranks all eligible symbols on as_of_date strictly using completed bars ts <= as_of_date.
    Top performer gets 99, worst gets 1.
    """
    if benchmark is None or benchmark.empty or not stocks:
        return {}

    weights = lookback_weights or {20: 0.40, 60: 0.40, 120: 0.20}

    # Extract benchmark session-to-close series up to as_of_date
    b_dict = {}
    for t_val, c_val in zip(benchmark["ts"], benchmark["close"]):
        try:
            c_flt = float(c_val)
        except (ValueError, TypeError):
            continue
        if c_flt <= 0:
            continue
        if isinstance(t_val, date) and not isinstance(t_val, datetime):
            dt_val = t_val
        else:
            try:
                dt_val = t_val.date() if hasattr(t_val, "date") else pd.Timestamp(t_val).date()
            except Exception:
                continue
        if dt_val <= as_of_date:
            b_dict[dt_val] = c_flt

    b_sessions = sorted(b_dict.keys())
    if len(b_sessions) < min_common_sessions:
        return {}

    raw_scores: dict[str, float] = {}

    for symbol, frame in stocks.items():
        if frame is None or frame.empty:
            continue
        s_dict = {}
        for t_val, c_val in zip(frame["ts"], frame["close"]):
            try:
                c_flt = float(c_val)
            except (ValueError, TypeError):
                continue
            if c_flt <= 0:
                continue
            if isinstance(t_val, date) and not isinstance(t_val, datetime):
                dt_val = t_val
            else:
                try:
                    dt_val = t_val.date() if hasattr(t_val, "date") else pd.Timestamp(t_val).date()
                except Exception:
                    continue
            if dt_val <= as_of_date:
                s_dict[dt_val] = c_flt

        common_sessions = [d for d in b_sessions if d in s_dict]
        total_common = len(common_sessions)
        if total_common < min_common_sessions:
            continue

        avail_lookbacks = [n for n in sorted(weights.keys()) if total_common >= n + 1]
        if not avail_lookbacks:
            continue

        w_sum = sum(float(weights[n]) for n in avail_lookbacks)
        if w_sum <= 0:
            continue

        last_sess = common_sessions[-1]
        s_end = s_dict[last_sess]
        b_end = b_dict[last_sess]
        if s_end <= 0 or b_end <= 0:
            continue

        score = 0.0
        valid = True
        for n in avail_lookbacks:
            start_sess = common_sessions[-(n + 1)]
            s_start = s_dict[start_sess]
            b_start = b_dict[start_sess]
            if s_start <= 0 or b_start <= 0:
                valid = False
                break
            s_ret = (s_end / s_start - 1.0) * 100.0
            b_ret = (b_end / b_start - 1.0) * 100.0
            score += (float(weights[n]) / w_sum) * (s_ret - b_ret)

        if valid:
            raw_scores[symbol] = score

    if not raw_scores:
        return {}

    k = len(raw_scores)
    if k == 1:
        return {next(iter(raw_scores)): 50}

    # Percentile ranking into integer 1..99
    series = pd.Series(raw_scores)
    ranks = series.rank(method="average", ascending=True)
    min_rank = 1.0
    max_rank = float(k)

    if max_rank > min_rank:
        norm = (ranks - min_rank) / (max_rank - min_rank)
        ratings = (1 + np.round(norm * 98)).astype(int)
    else:
        ratings = pd.Series(50, index=series.index, dtype=int)

    return ratings.to_dict()

