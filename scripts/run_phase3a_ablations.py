#!/usr/bin/env python3
"""Run automated Phase 3A offline ablation suite over 2026-06-01 to 2026-11-20.

Starting from baseline Test C1 (2.0x ATR Stop / 1.5R Target, Long-Only, CLV off, max_hold_days: null).
Isolates ONE variable at a time before combining:
- P3-0: Baseline C1 Parity
- P3-1a: Regime Gate (Bear-Block) ['bullish', 'mixed']
- P3-1b: Regime Gate (Bullish-Only) ['bullish']
- P3-2a: RS Rating >= 60
- P3-2b: RS Rating >= 70
- P3-2c: RS Rating >= 80
- P3-3a: Trailing breakeven_1r (1.5R)
- P3-3b: Trailing breakeven_1r (2.0R)
- P3-3c: Trailing atr_trail (2.5R, 2.0x ATR trail)
- P3-4a: Best Regime + Best RS (1.5R)
- P3-4b: Best Regime + Best RS + Best Trail
"""
import copy
from datetime import date, datetime, timezone
import json
import math
from pathlib import Path
import pandas as pd

from src.config_loader import load_config
import src.backtest.cli as cli_mod
from src.scanner import Scanner

# Memoize Scanner._scan_stock so signal evaluation happens only once across the 495 symbols
scan_cache = {}
orig_scan = Scanner._scan_stock

def cached_scan(self, stock, frame, provisional=False, current_time=None, is_power_hour=False):
    ts_key = str(frame["ts"].iloc[-1]) if "ts" in frame.columns and not frame.empty else len(frame)
    key = (stock.symbol, ts_key)
    if key not in scan_cache:
        scan_cache[key] = orig_scan(
            self, stock, frame, provisional=provisional,
            current_time=current_time, is_power_hour=is_power_hour,
        )
    return scan_cache[key]

Scanner._scan_stock = cached_scan


def simulate_portfolio_5l(trades: list[dict], starting_capital: float = 500_000.0,
                           max_tranche: float = 100_000.0, max_risk: float = 5_000.0,
                           cost_pct: float = 0.0030) -> dict:
    """Realistic portfolio simulation: ₹5L capital, ₹1L tranche cap, ₹5k risk budget, 0.30% CNC cost."""
    if not trades:
        return {
            "starting_capital_inr": starting_capital,
            "ending_capital_inr": starting_capital,
            "net_pnl_inr": 0.0,
            "roi_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "trade_count": 0,
        }

    # Sort trades chronologically by entry_date, then exit_date
    sorted_trades = sorted(trades, key=lambda t: (t["entry_date"], t.get("symbol", "")))

    equity = starting_capital
    peak = starting_capital
    max_dd = 0.0
    total_net_pnl = 0.0

    trade_pnl_records = []
    for t in sorted_trades:
        entry_p = float(t["entry_price"])
        exit_p = float(t["exit_price"])
        stop_p = float(t.get("stop_reference", entry_p * 0.94))
        risk_dist = abs(entry_p - stop_p)
        if risk_dist <= 0:
            risk_dist = entry_p * 0.02

        qty_risk = math.floor(max_risk / risk_dist)
        qty_tranche = math.floor(max_tranche / entry_p)
        qty = max(1, min(qty_risk, qty_tranche))

        gross_pnl = qty * (exit_p - entry_p)
        turnover = (qty * entry_p) + (qty * exit_p)
        trade_cost = turnover * (cost_pct / 2.0)  # 0.30% total round-trip
        net_pnl = gross_pnl - trade_cost

        total_net_pnl += net_pnl
        equity += net_pnl
        peak = max(peak, equity)
        dd = (peak - equity) / peak * 100.0 if peak > 0 else 0.0
        max_dd = max(max_dd, dd)

        trade_pnl_records.append({
            "symbol": t["symbol"],
            "entry_date": t["entry_date"],
            "exit_date": t["exit_date"],
            "quantity": qty,
            "notional_inr": round(qty * entry_p, 2),
            "net_pnl_inr": round(net_pnl, 2),
            "running_equity": round(equity, 2),
        })

    roi_pct = (total_net_pnl / starting_capital) * 100.0
    return {
        "starting_capital_inr": round(starting_capital, 2),
        "ending_capital_inr": round(equity, 2),
        "net_pnl_inr": round(total_net_pnl, 2),
        "roi_pct": round(roi_pct, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "trade_count": len(sorted_trades),
    }


def compute_monthly_breakdown(trades: list[dict]) -> dict:
    """Group trades by signal month and calculate performance metrics."""
    if not trades:
        return {}

    by_month: dict[str, list[dict]] = {}
    for t in trades:
        m = t["signal_date"][:7]
        by_month.setdefault(m, []).append(t)

    result = {}
    for m in sorted(by_month.keys()):
        m_trades = by_month[m]
        tc = len(m_trades)
        wins = sum(1 for t in m_trades if t["net_return_pct"] > 0)
        losses = sum(1 for t in m_trades if t["net_return_pct"] < 0)
        wr = round(wins / tc * 100.0, 1) if tc else 0.0

        r_vals = [t["net_r_multiple"] for t in m_trades if t.get("net_r_multiple") is not None]
        exp_r = round(sum(r_vals) / len(r_vals), 2) if r_vals else 0.0

        pos = sum(t["net_return_pct"] for t in m_trades if t["net_return_pct"] > 0)
        neg = abs(sum(t["net_return_pct"] for t in m_trades if t["net_return_pct"] < 0))
        pf = round(pos / neg, 2) if neg > 0 else (999.0 if pos > 0 else 0.0)

        result[m] = {
            "trade_count": tc,
            "wins": wins,
            "losses": losses,
            "win_rate_pct": wr,
            "expectancy_r": exp_r,
            "profit_factor": pf,
        }
    return result


def analyze_stops(trades_csv_path: Path):
    if not trades_csv_path.exists():
        return {}
    df = pd.read_csv(trades_csv_path)
    if df.empty:
        return {"total_trades": 0}

    stops = df[df["exit_reason"].isin(["stop", "trailing_stop"])].copy()
    if stops.empty:
        return {"total_trades": len(df), "stops": 0, "stops_le_3d": 0, "stops_le_3d_pct": 0.0}

    stops["entry_date"] = pd.to_datetime(stops["entry_date"])
    stops["exit_date"] = pd.to_datetime(stops["exit_date"])
    stops["hold_days"] = (stops["exit_date"] - stops["entry_date"]).dt.days
    le_3d = (stops["hold_days"] <= 3).sum()
    return {
        "total_trades": len(df),
        "stops": len(stops),
        "stops_le_3d": int(le_3d),
        "stops_le_3d_pct": round(float(le_3d / len(stops) * 100.0), 2),
        "stops_pct_of_all_trades": round(float(len(stops) / len(df) * 100.0), 2),
        "stops_le_3d_pct_of_all_trades": round(float(le_3d / len(df) * 100.0), 2),
    }


def run_single_ablation(base_cfg: dict, name: str, run_id: str, out_dir: Path, overrides: dict) -> dict:
    current_cfg = copy.deepcopy(base_cfg)
    for path_keys, val in overrides.items():
        ref = current_cfg
        for k in path_keys[:-1]:
            ref = ref.setdefault(k, {})
        ref[path_keys[-1]] = val

    cli_mod.get_config = lambda c=current_cfg: c

    start_t = datetime.now(timezone.utc)
    res = cli_mod.run_backtest(
        start_date=date(2026, 6, 1),
        end_date=date(2026, 11, 20),
        symbols=None,
        out_dir=out_dir,
    )
    dur = (datetime.now(timezone.utc) - start_t).total_seconds()

    report_json_path = out_dir / "report.json"
    with open(report_json_path) as f:
        rep_data = json.load(f)

    trades = rep_data.get("trades", [])
    monthly = compute_monthly_breakdown(trades)
    sim_5l = simulate_portfolio_5l(trades)
    stop_stats = analyze_stops(out_dir / "trades.csv")

    summary = res.get("overall", {})
    record = {
        "id": run_id,
        "name": name,
        "hash": res.get("config_hash_sha256", "")[:8],
        "full_hash": res.get("config_hash_sha256", ""),
        "duration_sec": round(dur, 1),
        "out_dir": str(out_dir),
        "summary": {
            "trade_count": summary.get("trade_count", 0),
            "wins": summary.get("wins", 0),
            "losses": summary.get("losses", 0),
            "win_rate_pct": summary.get("win_rate_pct"),
            "expectancy_net_return_pct_per_trade": summary.get("expectancy_net_return_pct_per_trade"),
            "expectancy_r": summary.get("expectancy_r"),
            "profit_factor": summary.get("profit_factor"),
            "max_drawdown_pct_sequential_trade_equity": summary.get("max_drawdown_pct_sequential_trade_equity"),
        },
        "walk_forward": res.get("walk_forward", {}),
        "monthly_breakdown": monthly,
        "portfolio_5l_sim": sim_5l,
        "stop_stats": stop_stats,
    }
    print(f"[{run_id}] {name} => Trades: {record['summary']['trade_count']} | "
          f"WR: {record['summary']['win_rate_pct']}% | ExpR: {record['summary']['expectancy_r']} | "
          f"PF: {record['summary']['profit_factor']} | 5L ROI: {sim_5l['roi_pct']}% | "
          f"5L MaxDD: {sim_5l['max_drawdown_pct']}% | Hash: {record['hash']}", flush=True)
    return record


def main():
    base_cfg = load_config()

    # Base common overrides for all Phase 3A ablations
    # Locked baseline: apply_to_daily = false, allowed_directions = ["BUY"], stop = 2.0x ATR
    base_overrides = {
        ("guardrails", "wick_filter", "apply_to_daily"): False,
        ("backtest", "allowed_directions"): ["BUY"],
        ("backtest", "stop_atr_multiple"): 2.0,
        ("backtest", "max_hold_days"): None,
        ("market_context", "mode"): "informational",
        ("guardrails", "rs_filter", "enabled"): False,
        ("backtest", "trailing_stop_mode"): "none",
    }

    phase3a_runs = [
        # P3-0: Baseline C1 Parity
        {
            "id": "P3-0",
            "name": "Baseline C1 Parity (2.0x ATR / 1.5R)",
            "out_dir": Path("reports/backtests/phase3a/P3-0_baseline_c1"),
            "overrides": {
                **base_overrides,
                ("backtest", "target_r_multiple"): 1.5,
            },
        },
        # P3-1a: Regime Gate (Bear-Block)
        {
            "id": "P3-1a",
            "name": "Regime Gate (Bear-Block: ['bullish', 'mixed'])",
            "out_dir": Path("reports/backtests/phase3a/P3-1a_regime_bear_block"),
            "overrides": {
                **base_overrides,
                ("backtest", "target_r_multiple"): 1.5,
                ("market_context", "mode"): "benchmark_filter",
                ("market_context", "allowed_buy_regimes"): ["bullish", "mixed"],
            },
        },
        # P3-1b: Regime Gate (Bullish-Only)
        {
            "id": "P3-1b",
            "name": "Regime Gate (Bullish-Only: ['bullish'])",
            "out_dir": Path("reports/backtests/phase3a/P3-1b_regime_bullish_only"),
            "overrides": {
                **base_overrides,
                ("backtest", "target_r_multiple"): 1.5,
                ("market_context", "mode"): "benchmark_filter",
                ("market_context", "allowed_buy_regimes"): ["bullish"],
            },
        },
        # P3-2a: RS Rating >= 60
        {
            "id": "P3-2a",
            "name": "RS Filter >= 60",
            "out_dir": Path("reports/backtests/phase3a/P3-2a_rs_ge_60"),
            "overrides": {
                **base_overrides,
                ("backtest", "target_r_multiple"): 1.5,
                ("guardrails", "rs_filter", "enabled"): True,
                ("guardrails", "rs_filter", "min_rs_rating_buy"): 60,
                ("guardrails", "rs_filter", "unknown_policy"): "exclude",
            },
        },
        # P3-2b: RS Rating >= 70
        {
            "id": "P3-2b",
            "name": "RS Filter >= 70",
            "out_dir": Path("reports/backtests/phase3a/P3-2b_rs_ge_70"),
            "overrides": {
                **base_overrides,
                ("backtest", "target_r_multiple"): 1.5,
                ("guardrails", "rs_filter", "enabled"): True,
                ("guardrails", "rs_filter", "min_rs_rating_buy"): 70,
                ("guardrails", "rs_filter", "unknown_policy"): "exclude",
            },
        },
        # P3-2c: RS Rating >= 80
        {
            "id": "P3-2c",
            "name": "RS Filter >= 80",
            "out_dir": Path("reports/backtests/phase3a/P3-2c_rs_ge_80"),
            "overrides": {
                **base_overrides,
                ("backtest", "target_r_multiple"): 1.5,
                ("guardrails", "rs_filter", "enabled"): True,
                ("guardrails", "rs_filter", "min_rs_rating_buy"): 80,
                ("guardrails", "rs_filter", "unknown_policy"): "exclude",
            },
        },
        # P3-3a: Trailing breakeven_1r (1.5R Target)
        {
            "id": "P3-3a",
            "name": "Trailing breakeven_1r (1.5R Target)",
            "out_dir": Path("reports/backtests/phase3a/P3-3a_trailing_be_1_5r"),
            "overrides": {
                **base_overrides,
                ("backtest", "target_r_multiple"): 1.5,
                ("backtest", "trailing_stop_mode"): "breakeven_1r",
            },
        },
        # P3-3b: Trailing breakeven_1r (2.0R Target)
        {
            "id": "P3-3b",
            "name": "Trailing breakeven_1r (2.0R Target)",
            "out_dir": Path("reports/backtests/phase3a/P3-3b_trailing_be_2_0r"),
            "overrides": {
                **base_overrides,
                ("backtest", "target_r_multiple"): 2.0,
                ("backtest", "trailing_stop_mode"): "breakeven_1r",
            },
        },
        # P3-3c: Trailing atr_trail (2.5R Target)
        {
            "id": "P3-3c",
            "name": "Trailing atr_trail (2.5R Target, 2.0x ATR trail)",
            "out_dir": Path("reports/backtests/phase3a/P3-3c_trailing_atr_2_5r"),
            "overrides": {
                **base_overrides,
                ("backtest", "target_r_multiple"): 2.5,
                ("backtest", "trailing_stop_mode"): "atr_trail",
                ("backtest", "trail_atr_multiple"): 2.0,
            },
        },
    ]

    results = []

    print("\n" + "="*80)
    print("PHASE 3A ABLATION SUITE: RUNNING SINGLE-VARIABLE ISOLATION (STAGE 1)")
    print("="*80, flush=True)

    for run_spec in phase3a_runs:
        rec = run_single_ablation(
            base_cfg=base_cfg,
            name=run_spec["name"],
            run_id=run_spec["id"],
            out_dir=run_spec["out_dir"],
            overrides=run_spec["overrides"],
        )
        results.append(rec)

    # ─────────────────────────────────────────────────────────────────────────
    # Stage 2: Combine Best Performers (P3-4a and P3-4b)
    # ─────────────────────────────────────────────────────────────────────────
    print("\n" + "="*80)
    print("PHASE 3A ABLATION SUITE: COMBINING BEST PERFORMERS (STAGE 2)")
    print("="*80, flush=True)

    # Select best regime from P3-1a vs P3-1b
    p3_1a = next(r for r in results if r["id"] == "P3-1a")
    p3_1b = next(r for r in results if r["id"] == "P3-1b")
    pf_1a = p3_1a["summary"]["profit_factor"] or 0.0
    pf_1b = p3_1b["summary"]["profit_factor"] or 0.0
    best_regime_id = "P3-1a" if pf_1a >= pf_1b else "P3-1b"
    best_regime_allowed = ["bullish", "mixed"] if best_regime_id == "P3-1a" else ["bullish"]
    print(f"Selected Best Regime: {best_regime_id} (PF: {max(pf_1a, pf_1b)})")

    # Select best RS filter among P3-2a, P3-2b, P3-2c
    rs_candidates = [r for r in results if r["id"] in ("P3-2a", "P3-2b", "P3-2c")]
    best_rs_rec = max(rs_candidates, key=lambda r: (r["summary"]["profit_factor"] or 0.0, r["summary"]["expectancy_r"] or 0.0))
    best_rs_id = best_rs_rec["id"]
    best_rs_min = 60 if best_rs_id == "P3-2a" else (70 if best_rs_id == "P3-2b" else 80)
    print(f"Selected Best RS Filter: {best_rs_id} (min_rs: {best_rs_min}, PF: {best_rs_rec['summary']['profit_factor']})")

    # Select best trailing stop among P3-3a, P3-3b, P3-3c
    trail_candidates = [r for r in results if r["id"] in ("P3-3a", "P3-3b", "P3-3c")]
    best_trail_rec = max(trail_candidates, key=lambda r: (r["summary"]["profit_factor"] or 0.0, r["summary"]["expectancy_r"] or 0.0))
    best_trail_id = best_trail_rec["id"]
    if best_trail_id == "P3-3a":
        best_trail_mode = "breakeven_1r"
        best_target = 1.5
    elif best_trail_id == "P3-3b":
        best_trail_mode = "breakeven_1r"
        best_target = 2.0
    else:
        best_trail_mode = "atr_trail"
        best_target = 2.5
    print(f"Selected Best Trailing Stop: {best_trail_id} (mode: {best_trail_mode}, target: {best_target}R, PF: {best_trail_rec['summary']['profit_factor']})")

    # P3-4a: Best Regime + Best RS (1.5R target, no trail)
    rec_4a = run_single_ablation(
        base_cfg=base_cfg,
        name=f"Best Regime ({best_regime_id}) + Best RS ({best_rs_id}) (1.5R)",
        run_id="P3-4a",
        out_dir=Path("reports/backtests/phase3a/P3-4a_best_regime_and_rs"),
        overrides={
            **base_overrides,
            ("backtest", "target_r_multiple"): 1.5,
            ("market_context", "mode"): "benchmark_filter",
            ("market_context", "allowed_buy_regimes"): best_regime_allowed,
            ("guardrails", "rs_filter", "enabled"): True,
            ("guardrails", "rs_filter", "min_rs_rating_buy"): best_rs_min,
            ("guardrails", "rs_filter", "unknown_policy"): "exclude",
        },
    )
    results.append(rec_4a)

    # P3-4b: Best Regime + Best RS + Best Trail
    rec_4b = run_single_ablation(
        base_cfg=base_cfg,
        name=f"Best Regime ({best_regime_id}) + Best RS ({best_rs_id}) + Best Trail ({best_trail_id})",
        run_id="P3-4b",
        out_dir=Path("reports/backtests/phase3a/P3-4b_best_regime_rs_trail"),
        overrides={
            **base_overrides,
            ("backtest", "target_r_multiple"): best_target,
            ("backtest", "trailing_stop_mode"): best_trail_mode,
            ("market_context", "mode"): "benchmark_filter",
            ("market_context", "allowed_buy_regimes"): best_regime_allowed,
            ("guardrails", "rs_filter", "enabled"): True,
            ("guardrails", "rs_filter", "min_rs_rating_buy"): best_rs_min,
            ("guardrails", "rs_filter", "unknown_policy"): "exclude",
            **({("backtest", "trail_atr_multiple"): 2.0} if best_trail_mode == "atr_trail" else {}),
        },
    )
    results.append(rec_4b)

    # Consolidated comparison summary JSON
    summary_path = Path("reports/backtests/phase3a/phase3a_comparison_summary.json")
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    with open(summary_path, "w") as f:
        json.dump(results, f, indent=2)

    print("\n" + "="*80)
    print("ALL PHASE 3A ABLATIONS COMPLETED SUCCESSFULLY!")
    print(f"Consolidated results saved to: {summary_path}")
    print("="*80, flush=True)


if __name__ == "__main__":
    main()
