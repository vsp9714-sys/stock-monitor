"""No-lookahead event replay with explicit daily-bar execution assumptions."""
from __future__ import annotations

import math
from datetime import date
from typing import Any, Callable

import pandas as pd

from src.risk import risk_reference

SignalEvaluator = Callable[[str, pd.DataFrame], dict[str, Any] | None]


def _session_dates(frame: pd.DataFrame) -> pd.Series:
    return pd.to_datetime(frame["ts"], errors="coerce").map(lambda value: value.date() if not pd.isna(value) else None)


def simulate_period(
    candles: dict[str, pd.DataFrame],
    evaluate: SignalEvaluator,
    *,
    start_date: date,
    end_date: date,
    config: dict[str, Any],
    membership_by_date: dict[date, set[str]] | None = None,
) -> dict[str, Any]:
    """Simulate next-open entries and ATR stops/targets using only bars available then.

    When a daily candle hits both stop and target, stop is assumed first. Gap-through
    stops fill at the opening price. Trade returns are reported on full notional;
    drawdown compounds the closed-trade returns sequentially (not a portfolio curve).
    """
    bt = config.get("backtest", {})
    risk_cfg = config.get("risk", {})
    atr_period = int(bt.get("atr_period", risk_cfg.get("atr_period", 14)))
    stop_multiple = float(bt.get("stop_atr_multiple", risk_cfg.get("stop_atr_multiple", 1.5)))
    target_multiple = float(bt.get("target_r_multiple", risk_cfg.get("target_r_multiple", 2.0)))
    fee_bps = float(bt.get("fee_bps_per_side", 5.0))
    slippage_bps = float(bt.get("slippage_bps_per_side", 5.0))
    raw_max_hold = bt.get("max_hold_days")
    max_hold_days: int | None = None
    if raw_max_hold is not None:
        try:
            val = int(raw_max_hold)
            if val > 0:
                max_hold_days = val
        except (ValueError, TypeError):
            max_hold_days = None

    raw_allowed = bt.get("allowed_directions")
    allowed_directions: set[str] | None = None
    if raw_allowed is not None:
        allowed_directions = {str(d).upper() for d in raw_allowed}

    raw_max_risk_dist = bt.get("max_risk_distance_pct", risk_cfg.get("max_risk_distance_pct"))
    max_risk_distance_pct: float | None = None
    if raw_max_risk_dist is not None:
        try:
            val = float(raw_max_risk_dist)
            if math.isfinite(val) and val > 0:
                max_risk_distance_pct = val
        except (ValueError, TypeError):
            max_risk_distance_pct = None

    trailing_stop_mode = str(bt.get("trailing_stop_mode", "none")).lower()
    trailing_trigger_r = float(bt.get("trailing_trigger_r", 1.0))
    breakeven_buffer_r = float(bt.get("breakeven_buffer_r", 0.0))
    trail_atr_multiple = float(bt.get("trail_atr_multiple", 2.0))

    trades: list[dict[str, Any]] = []
    total_symbol_sessions = 0
    exposure_sessions = 0
    skipped = {"no_next_bar": 0, "atr_unavailable": 0, "invalid_entry": 0, "risk_distance_exceeds_max": 0}

    for symbol, original in sorted(candles.items()):
        if original is None or original.empty:
            continue
        frame = original.copy().sort_values("ts").reset_index(drop=True)
        sessions = _session_dates(frame)
        mask = sessions.notna() & (sessions <= end_date)
        frame = frame.loc[mask].reset_index(drop=True)
        sessions = _session_dates(frame)
        total_symbol_sessions += int(((sessions >= start_date) & (sessions <= end_date)).sum())
        i = 29  # Match the live signal evaluator's minimum history requirement.
        while i < len(frame) - 1:
            signal_day = sessions.iloc[i]
            if signal_day is None or signal_day < start_date or signal_day > end_date:
                i += 1
                continue
            if membership_by_date is not None and signal_day in membership_by_date and symbol not in membership_by_date[signal_day]:
                i += 1
                continue
            # The evaluator sees only completed data through the current close.
            signal = evaluate(symbol, frame.iloc[: i + 1].copy())
            if not signal or signal.get("direction") not in {"BUY", "SELL"}:
                i += 1
                continue
            if allowed_directions is not None and signal.get("direction") not in allowed_directions:
                i += 1
                continue
            entry_idx = i + 1
            entry_row = frame.iloc[entry_idx]
            raw_entry = float(entry_row["open"])
            if raw_entry <= 0 or not pd.notna(raw_entry):
                skipped["invalid_entry"] += 1
                i += 1
                continue
            risk_ref_cfg = {
                "enabled": True, "atr_period": atr_period,
                "stop_atr_multiple": stop_multiple, "target_r_multiple": target_multiple,
            }
            if max_risk_distance_pct is not None:
                risk_ref_cfg["max_risk_distance_pct"] = max_risk_distance_pct
            risk = risk_reference(
                frame.iloc[: i + 1], entry_price=raw_entry,
                direction=signal["direction"],
                config=risk_ref_cfg,
            )
            if risk.get("status") != "available":
                if risk.get("reason") == "risk_distance_exceeds_max":
                    skipped["risk_distance_exceeds_max"] += 1
                else:
                    skipped["atr_unavailable"] += 1
                i += 1
                continue
            side = 1 if signal["direction"] == "BUY" else -1
            slip = slippage_bps / 10_000.0
            entry_fill = raw_entry * (1 + slip if side == 1 else 1 - slip)
            stop = float(risk["stop_reference"])
            target = float(risk["target_reference"])
            risk_dist = float(risk["risk_distance"])
            atr_val = float(risk["atr"])
            current_stop = stop
            is_ratcheted = False
            triggered_1r = False
            exit_idx = len(frame) - 1
            exit_price = float(frame.iloc[-1]["close"])
            exit_reason = "period_end"
            for j in range(entry_idx, len(frame)):
                bar = frame.iloc[j]
                open_price, high, low, close_p = map(float, (bar["open"], bar["high"], bar["low"], bar["close"]))
                if side == 1:
                    stop_hit, target_hit = low <= current_stop, high >= target
                    if stop_hit:
                        exit_price = min(open_price, current_stop)
                        exit_reason, exit_idx = "trailing_stop" if is_ratcheted else "stop", j
                        break
                    if target_hit:
                        exit_price = target
                        exit_reason, exit_idx = "target", j
                        break
                else:
                    stop_hit, target_hit = high >= current_stop, low <= target
                    if stop_hit:
                        exit_price = max(open_price, current_stop)
                        exit_reason, exit_idx = "trailing_stop" if is_ratcheted else "stop", j
                        break
                    if target_hit:
                        exit_price = target
                        exit_reason, exit_idx = "target", j
                        break
                if max_hold_days is not None and (j - entry_idx + 1) >= max_hold_days:
                    exit_price = close_p
                    exit_reason, exit_idx = "time_expiry", j
                    break

                # End-of-bar update: ratchet stop for bar j + 1 onward (zero intraday lookahead)
                if trailing_stop_mode == "breakeven_1r":
                    if side == 1:
                        if high >= raw_entry + trailing_trigger_r * risk_dist:
                            new_stop = max(current_stop, raw_entry + breakeven_buffer_r * risk_dist)
                            if new_stop > current_stop:
                                current_stop = new_stop
                                is_ratcheted = True
                    else:
                        if low <= raw_entry - trailing_trigger_r * risk_dist:
                            new_stop = min(current_stop, raw_entry - breakeven_buffer_r * risk_dist)
                            if new_stop < current_stop:
                                current_stop = new_stop
                                is_ratcheted = True
                elif trailing_stop_mode == "atr_trail":
                    if side == 1:
                        if high >= raw_entry + trailing_trigger_r * risk_dist:
                            triggered_1r = True
                        if triggered_1r:
                            trail_stop = close_p - trail_atr_multiple * atr_val
                            new_stop = max(current_stop, trail_stop)
                            if new_stop > current_stop:
                                current_stop = new_stop
                                is_ratcheted = True
                    else:
                        if low <= raw_entry - trailing_trigger_r * risk_dist:
                            triggered_1r = True
                        if triggered_1r:
                            trail_stop = close_p + trail_atr_multiple * atr_val
                            new_stop = min(current_stop, trail_stop)
                            if new_stop < current_stop:
                                current_stop = new_stop
                                is_ratcheted = True

            exit_fill = exit_price * (1 - slip if side == 1 else 1 + slip)
            gross_pct = side * (exit_price / raw_entry - 1.0) * 100.0
            # Slippage is represented in the actual fills above; charge fees
            # separately on each side to avoid counting slippage twice.
            net_pct = side * (exit_fill / entry_fill - 1.0) * 100.0 - 2.0 * fee_bps / 100.0
            cost_pct = gross_pct - net_pct
            risk_pct = float(risk["risk_distance"]) / raw_entry * 100.0
            trades.append({
                "symbol": symbol, "direction": signal["direction"],
                "signal_date": signal_day.isoformat(),
                "entry_date": sessions.iloc[entry_idx].isoformat(),
                "entry_price": entry_fill,
                "exit_date": sessions.iloc[exit_idx].isoformat(),
                "exit_price": exit_fill, "exit_reason": exit_reason,
                "atr_at_signal": risk["atr"], "stop_reference": stop,
                "target_reference": target,
                "benchmark_regime_at_signal": signal.get("benchmark_regime_at_signal"),
                "rs_rating_at_signal": signal.get("rs_rating_at_signal"),
                "gross_return_pct": gross_pct,
                "cost_pct": cost_pct, "net_return_pct": net_pct,
                "net_r_multiple": net_pct / risk_pct if risk_pct > 0 else None,
                "holding_sessions": exit_idx - entry_idx + 1,
            })
            exposure_sessions += exit_idx - entry_idx + 1
            i = max(entry_idx, exit_idx) + 1

    trades.sort(key=lambda item: (item["exit_date"], item["symbol"], item["entry_date"]))
    returns = [trade["net_return_pct"] / 100.0 for trade in trades]
    equity = peak = 1.0
    max_drawdown = 0.0
    for result in returns:
        equity *= 1.0 + result
        peak = max(peak, equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)
    wins = sum(value > 0 for value in returns)
    losses = sum(value < 0 for value in returns)
    pos_sum = sum(value for value in returns if value > 0)
    neg_sum = abs(sum(value for value in returns if value < 0))
    profit_factor = round(pos_sum / neg_sum, 2) if neg_sum > 0 else None
    valid_r = [t["net_r_multiple"] for t in trades if t.get("net_r_multiple") is not None]
    expectancy_r = round(sum(valid_r) / len(valid_r), 2) if valid_r else None
    summary = {
        "trade_count": len(trades), "wins": wins, "losses": losses,
        "win_rate_pct": wins / len(trades) * 100 if trades else None,
        "expectancy_net_return_pct_per_trade": sum(value * 100 for value in returns) / len(returns) if returns else None,
        "mean_win_pct": sum(value * 100 for value in returns if value > 0) / wins if wins else None,
        "mean_loss_pct": sum(value * 100 for value in returns if value < 0) / losses if losses else None,
        "max_drawdown_pct_sequential_trade_equity": max_drawdown * 100,
        "exposure_pct_symbol_sessions": exposure_sessions / total_symbol_sessions * 100 if total_symbol_sessions else None,
        "closed_trade_equity_multiple_sequential": equity,
        "profit_factor": profit_factor,
        "expectancy_r": expectancy_r,
        "skipped": skipped,
    }
    return {"summary": summary, "trades": trades}


def benchmark_return(frame: pd.DataFrame | None, start_date: date, end_date: date) -> dict[str, Any]:
    """Compute cached benchmark buy-and-hold return, or report unavailable."""
    if frame is None or frame.empty:
        return {"status": "unavailable", "reason": "benchmark_cache_missing"}
    dates = _session_dates(frame)
    selected = frame.loc[dates.notna() & (dates >= start_date) & (dates <= end_date)]
    if len(selected) < 2:
        return {"status": "unavailable", "reason": "benchmark_period_not_covered"}
    first, last = float(selected["close"].iloc[0]), float(selected["close"].iloc[-1])
    if first <= 0:
        return {"status": "unavailable", "reason": "invalid_benchmark_close"}
    return {"status": "available", "start": _session_dates(selected).iloc[0].isoformat(),
            "end": _session_dates(selected).iloc[-1].isoformat(),
            "total_return_pct": (last / first - 1.0) * 100.0}
