#!/usr/bin/env python3
"""Run verification backtests Test C (without CLV interference, Long-Only)."""
import copy
from datetime import date, datetime, timezone
import json
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

    scenarios = [
        {
            "name": "Test C1 (Stop 2.0x, Target 1.5R [3.0x ATR], CLV off, Long-Only)",
            "out_dir": Path("reports/backtests/verification_test_c1_stop2_0_target1_5"),
            "overrides": {
                ("guardrails", "wick_filter", "apply_to_daily"): False,
                ("backtest", "allowed_directions"): ["BUY"],
                ("backtest", "stop_atr_multiple"): 2.0,
                ("backtest", "target_r_multiple"): 1.5,
                ("backtest", "max_hold_days"): None,
            },
        },
        {
            "name": "Test C2 (Stop 1.5x, Target 2.0R [3.0x ATR], CLV off, Long-Only)",
            "out_dir": Path("reports/backtests/verification_test_c2_stop1_5_target2_0"),
            "overrides": {
                ("guardrails", "wick_filter", "apply_to_daily"): False,
                ("backtest", "allowed_directions"): ["BUY"],
                ("backtest", "stop_atr_multiple"): 1.5,
                ("backtest", "target_r_multiple"): 2.0,
                ("backtest", "max_hold_days"): None,
            },
        },
        {
            "name": "Test C3 (Stop 1.5x, Target 1.5R [2.25x ATR], CLV off, Long-Only)",
            "out_dir": Path("reports/backtests/verification_test_c3_stop1_5_target1_5"),
            "overrides": {
                ("guardrails", "wick_filter", "apply_to_daily"): False,
                ("backtest", "allowed_directions"): ["BUY"],
                ("backtest", "stop_atr_multiple"): 1.5,
                ("backtest", "target_r_multiple"): 1.5,
                ("backtest", "max_hold_days"): None,
            },
        },
        {
            "name": "Test C4 (Stop 2.0x, Target 2.0R [4.0x ATR], CLV off, Long-Only)",
            "out_dir": Path("reports/backtests/verification_test_c4_stop2_0_target2_0"),
            "overrides": {
                ("guardrails", "wick_filter", "apply_to_daily"): False,
                ("backtest", "allowed_directions"): ["BUY"],
                ("backtest", "stop_atr_multiple"): 2.0,
                ("backtest", "target_r_multiple"): 2.0,
                ("backtest", "max_hold_days"): None,
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

    summary_path = Path("reports/backtests/test_c_comparison_summary.json")
    with open(summary_path, "w") as f:
        json.dump(results, f, indent=2)

    print("\n" + "="*80)
    print("TEST C SUITE COMPLETED. Results saved to:", summary_path)
    print("="*80)

if __name__ == "__main__":
    main()
