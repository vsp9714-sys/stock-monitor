"""
Run 3-point parity verification backtests:
1. P3-5_ytd_cap_8pct (2026-01-01 to 2026-11-20, max_risk_distance_pct=8.0)
2. P3-5_blind_h1_cap_8pct (2026-01-01 to 2026-05-31, max_risk_distance_pct=8.0)
3. P3-5_ytd_cap_6pct (2026-01-01 to 2026-11-20, max_risk_distance_pct=6.0)

Saves detailed reports under reports/backtests/phase3a/risk_cap_parity/
and consolidated summary.json.
"""
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sys

from scripts.run_h1_and_ytd_verification import run_test

def main():
    out_base = Path("reports/backtests/phase3a/risk_cap_parity")
    out_base.mkdir(parents=True, exist_ok=True)
    results = []

    # 1. P3-5_ytd_cap_8pct
    res_8_ytd = run_test(
        name="Locked Production Full YTD with 8% Risk Cap",
        test_id="P3-5_ytd_cap_8pct",
        start_d=date(2026, 1, 1),
        end_d=date(2026, 11, 20),
        out_dir=out_base / "P3-5_ytd_cap_8pct",
        cfg_overrides={
            ("risk", "max_risk_distance_pct"): 8.0,
            ("backtest", "max_risk_distance_pct"): 8.0,
        },
    )
    results.append(res_8_ytd)

    # 2. P3-5_blind_h1_cap_8pct
    res_8_h1 = run_test(
        name="Locked Production Blind H1 with 8% Risk Cap",
        test_id="P3-5_blind_h1_cap_8pct",
        start_d=date(2026, 1, 1),
        end_d=date(2026, 5, 31),
        out_dir=out_base / "P3-5_blind_h1_cap_8pct",
        cfg_overrides={
            ("risk", "max_risk_distance_pct"): 8.0,
            ("backtest", "max_risk_distance_pct"): 8.0,
        },
    )
    results.append(res_8_h1)

    # 3. P3-5_ytd_cap_6pct
    res_6_ytd = run_test(
        name="Locked Production Full YTD with 6% Risk Cap (Reference)",
        test_id="P3-5_ytd_cap_6pct",
        start_d=date(2026, 1, 1),
        end_d=date(2026, 11, 20),
        out_dir=out_base / "P3-5_ytd_cap_6pct",
        cfg_overrides={
            ("risk", "max_risk_distance_pct"): 6.0,
            ("backtest", "max_risk_distance_pct"): 6.0,
        },
    )
    results.append(res_6_ytd)

    summary_path = out_base / "summary.json"
    with open(summary_path, "w") as f:
        json.dump({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "results": results,
        }, f, indent=2)

    print(f"\nSaved consolidated summary to {summary_path}")

    # Print summary table
    print("\n" + "=" * 110)
    print("PARITY VERIFICATION SUMMARY: RISK DISTANCE CAP ABLATIONS (FIXED 5-SLOT ₹5L SIMULATOR)")
    print("=" * 110)
    print(f"{'Test ID':<26} | {'Period':<23} | {'Trades':<6} | {'WR%':<6} | {'PF':<5} | {'Exp(R)':<6} | {'5L ROI%':<8} | {'5L Net PnL':<12} | {'5L MaxDD%':<9}")
    print("-" * 110)
    for r in results:
        ov = r["overall"]
        sim = r["portfolio_5l"]
        pf_str = f"{ov['profit_factor']:.2f}" if ov.get("profit_factor") is not None else "N/A"
        expr_str = f"{ov['expectancy_r']:.2f}" if ov.get("expectancy_r") is not None else "N/A"
        print(f"{r['id']:<26} | {r['period']:<23} | {ov['trade_count']:<6} | {ov['win_rate_pct']:<6.1f} | {pf_str:<5} | {expr_str:<6} | {sim['roi_pct']:<8.2f} | ₹{sim['net_pnl_inr']:<11,.2f} | {sim['max_drawdown_pct']:<9.2f}")
    print("=" * 110)

if __name__ == "__main__":
    main()
