"""Deterministic risk references derived from completed daily candles.

These values are explanatory estimates only. This module has no broker or
order-routing integration.
"""
from __future__ import annotations

import math
from typing import Any

import pandas as pd


def atr_value(frame: pd.DataFrame, period: int = 14) -> float | None:
    """Return simple-average true range from the last completed bars."""
    if frame is None or period < 1 or len(frame) < period + 1:
        return None
    data = frame[["high", "low", "close"]].apply(pd.to_numeric, errors="coerce")
    if data.isna().any().any() or not data.map(math.isfinite).all().all():
        return None
    previous_close = data["close"].shift(1)
    true_range = pd.concat(
        [data["high"] - data["low"], (data["high"] - previous_close).abs(),
         (data["low"] - previous_close).abs()], axis=1,
    ).max(axis=1).iloc[1:]
    if len(true_range) < period:
        return None
    value = float(true_range.tail(period).mean())
    return value if math.isfinite(value) and value > 0 else None


def risk_reference(
    frame: pd.DataFrame,
    *,
    entry_price: float,
    direction: str,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create ATR-based stop/target references; sizing is omitted unless configured."""
    cfg = config or {}
    if not cfg.get("enabled", True):
        return {"status": "disabled", "label": "model reference only; not an order"}
    try:
        entry = float(entry_price)
        period = int(cfg.get("atr_period", 14))
        stop_multiple = float(cfg.get("stop_atr_multiple", 1.5))
        reward_multiple = float(cfg.get("target_r_multiple", 2.0))
    except (TypeError, ValueError):
        return {"status": "unavailable", "reason": "invalid_risk_configuration"}
    if not math.isfinite(entry) or entry <= 0 or direction not in {"BUY", "SELL"}:
        return {"status": "unavailable", "reason": "invalid_entry_or_direction"}
    if period < 1 or not math.isfinite(stop_multiple) or stop_multiple <= 0 or not math.isfinite(reward_multiple) or reward_multiple <= 0:
        return {"status": "unavailable", "reason": "invalid_risk_configuration"}
    atr = atr_value(frame, period)
    if atr is None:
        return {"status": "unavailable", "reason": "atr_history_unavailable", "atr_period": period}
    distance = atr * stop_multiple
    stop = entry - distance if direction == "BUY" else entry + distance
    target = entry + distance * reward_multiple if direction == "BUY" else entry - distance * reward_multiple
    if min(stop, target) <= 0 or distance <= 0:
        return {"status": "unavailable", "reason": "non_positive_risk_level", "atr": atr}
    result: dict[str, Any] = {
        "status": "available", "label": "model reference only; not an order",
        "method": "completed_daily_ATR", "atr_period": period, "atr": atr,
        "stop_atr_multiple": stop_multiple, "target_r_multiple": reward_multiple,
        "entry_reference": entry, "stop_reference": stop,
        "target_reference": target, "risk_distance": distance,
        "reward_risk_ratio": reward_multiple,
    }
    capital = cfg.get("account_capital_inr")
    risk_pct = cfg.get("risk_percent")
    if capital is not None and risk_pct is not None:
        try:
            capital, risk_pct = float(capital), float(risk_pct)
            if not math.isfinite(capital) or not math.isfinite(risk_pct) or capital <= 0 or risk_pct <= 0:
                result["position_size_status"] = "unavailable_invalid_account_config"
            else:
                result["risk_budget_inr"] = capital * risk_pct / 100.0
                result["quantity"] = max(0, math.floor(result["risk_budget_inr"] / distance))
                result["position_size_status"] = "available" if result["quantity"] > 0 else "unavailable_below_one_share"
        except (TypeError, ValueError, OverflowError):
            result["position_size_status"] = "unavailable_invalid_account_config"
    else:
        result["position_size_status"] = "unconfigured"
    return result
