#!/usr/bin/env python3
"""Run backtest ablations Test A and Test B and compare against baseline hash a6258e9f."""
import copy
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sys
import pandas as pd

from src.config_loader import load_config
import src.backtest.cli as cli_mod
from src.scanner import Scanner

# Memoize Scanner._scan_stock to avoid re-evaluating indicators 4 times across 495 symbols
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

def analyze_stops(trades_csv_path: Path):
    if not trades_csv_path.exists():
        return {}
    df = pd.read_csv(trades_csv_path)
    if df.empty:
        return {"total_trades": 0}
    
    stops = df[df["exit_reason"] == "stop"].copy()
    if stops.empty:
        return {
            "total_trades": len(df),
            "stops": 0,
            "stops_le_3d": 0,
            "stops_le_3d_pct": 0.0,
        }
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

def main():
    base_cfg = load_config()

    # Define test matrix
    # Baseline comparison: reports/backtests/20261122T180350Z (hash a6258e9f)
    scenarios = [
        {
            "name": "Test A1 (CLV Daily + Stop 2.0x ATR + Target 2.0R, Long-Only)",
            "out_dir": Path("reports/backtests/ablation_test_a_2_0r"),
            "overrides": {
                ("guardrails", "wick_filter", "apply_to_daily"): True,
                ("backtest", "allowed_directions"): ["BUY"],
                ("backtest", "stop_atr_multiple"): 2.0,
                ("backtest", "target_r_multiple"): 2.0,
                ("backtest", "max_hold_days"): None,
            },
        },
        {
            "name": "Test A2 (CLV Daily + Stop 2.0x ATR + Target 1.5R, Long-Only)",
            "out_dir": Path("reports/backtests/ablation_test_a_1_5r"),
            "overrides": {
                ("guardrails", "wick_filter", "apply_to_daily"): True,
                ("backtest", "allowed_directions"): ["BUY"],
                ("backtest", "stop_atr_multiple"): 2.0,
                ("backtest", "target_r_multiple"): 1.5,
                ("backtest", "max_hold_days"): None,
            },
        },
        {
            "name": "Test B1 (Test A1 + Max Hold 20 Sessions, Long-Only)",
            "out_dir": Path("reports/backtests/ablation_test_b_2_0r"),
            "overrides": {
                ("guardrails", "wick_filter", "apply_to_daily"): True,
                ("backtest", "allowed_directions"): ["BUY"],
                ("backtest", "stop_atr_multiple"): 2.0,
                ("backtest", "target_r_multiple"): 2.0,
                ("backtest", "max_hold_days"): 20,
            },
        },
        {
            "name": "Test B2 (Test A2 + Max Hold 20 Sessions, Long-Only)",
            "out_dir": Path("reports/backtests/ablation_test_b_1_5r"),
            "overrides": {
                ("guardrails", "wick_filter", "apply_to_daily"): True,
                ("backtest", "allowed_directions"): ["BUY"],
                ("backtest", "stop_atr_multiple"): 2.0,
                ("backtest", "target_r_multiple"): 1.5,
                ("backtest", "max_hold_days"): 20,
            },
        },
    ]

    results = []

    for sc in scenarios:
        print(f"\n{'='*70}\nRunning {sc['name']}...\n{'='*70}", flush=True)
        current_cfg = copy.deepcopy(base_cfg)
        for path_keys, val in sc["overrides"].items():
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
            out_dir=sc["out_dir"],
        )
        dur = (datetime.now(timezone.utc) - start_t).total_seconds()
        print(f"Completed {sc['name']} in {dur:.1f}s. Config Hash: {res.get('config_hash_sha256', '')[:8]}", flush=True)

        stop_stats = analyze_stops(sc["out_dir"] / "trades.csv")
        results.append({
            "name": sc["name"],
            "hash": res.get("config_hash_sha256", "")[:8],
            "full_hash": res.get("config_hash_sha256", ""),
            "duration_sec": round(dur, 1),
            "out_dir": str(sc["out_dir"]),
            "summary": res.get("overall", {}),
            "walk_forward": res.get("walk_forward", {}),
            "stop_stats": stop_stats,
        })

    # Save consolidated ablation summary
    summary_path = Path("reports/backtests/ablation_comparison_summary.json")
    with open(summary_path, "w") as f:
        json.dump(results, f, indent=2)

    print("\n" + "="*80)
    print("ALL ABLATIONS COMPLETED SUCCESSFULLY. Results saved to:", summary_path)
    print("="*80)

if __name__ == "__main__":
    main()
