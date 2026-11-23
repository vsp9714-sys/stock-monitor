#!/usr/bin/env python3
"""Run Blind H1 (2026-01-01 to 2026-05-31) and Full YTD (2026-01-01 to 2026-11-20) backtests.

Tests:
1. P3-0_blind_h1_2026: Baseline reference (regime gate OFF, RS filter OFF, 2.0x ATR / 1.5R, Long-Only)
2. P3-5_blind_h1_2026: Locked production config (Bear-Block + RS >= 50, 2.0x ATR / 1.5R, Long-Only)
3. P3-5_ytd_2026: Full 11-month YTD locked production config

Also executes realistic 5-slot cash-constrained ₹5L portfolio simulation:
- ₹5,00,000 capital
- ₹1,00,000 max tranche
- ₹5,000 max risk budget
- 0.30% round-trip CNC costs
- Strict T+1 slot release
- RS-descending tie-breaker for concurrent entry signals
"""
import copy
from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path
import pandas as pd

from src.config_loader import load_config
import src.backtest.cli as cli_mod
from src.scanner import Scanner

# Memoize Scanner._scan_stock for fast multi-run execution
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

def enable_scan_cache():
    if Scanner._scan_stock is not cached_scan:
        Scanner._scan_stock = cached_scan


def simulate_5slot_portfolio(trades: list[dict], starting_capital: float = 500_000.0,
                             max_tranche: float = 100_000.0, max_risk: float = 5_000.0,
                             cost_pct: float = 0.0030) -> dict:
    """
    Realistic portfolio simulation: ₹5L capital, ₹1L tranche cap, ₹5k risk budget, 0.30% round-trip CNC cost.
    Enforces:
    - Strict 5-slot simultaneous cap (slot released when day > prev_exit_date for T+1 CNC settlement).
    - RS-descending tie-breaker on same-day entry candidates.
    - Sizing: qty = min(floor(max_risk / (2.0 * atr_at_signal)), floor(min(max_tranche, cash) / entry_price)).
    - Accounting invariant: ending_capital_inr == starting_capital_inr + sum(trade_pnl_inr).
    """
    if not trades:
        return {
            "starting_capital_inr": starting_capital,
            "ending_capital_inr": starting_capital,
            "net_pnl_inr": 0.0,
            "roi_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "trades_taken": 0,
            "trades_skipped_slots": 0,
            "trades_skipped_cash": 0,
            "total_signals": 0,
        }

    # Group trades by entry_date
    trades_by_entry: dict[str, list[dict]] = {}
    dates_seen = set()
    for t in trades:
        ed = str(t["entry_date"])
        xd = str(t["exit_date"])
        dates_seen.add(ed)
        dates_seen.add(xd)
        trades_by_entry.setdefault(ed, []).append(t)

    sorted_dates = sorted(dates_seen)
    if not sorted_dates:
        return {}

    all_calendar_days = []
    cur = datetime.fromisoformat(sorted_dates[0]).date()
    end = datetime.fromisoformat(sorted_dates[-1]).date()
    while cur <= end:
        all_calendar_days.append(cur.isoformat())
        cur += timedelta(days=1)

    cash = starting_capital
    peak_equity = starting_capital
    max_dd = 0.0
    trades_taken = 0
    skipped_slots = 0
    skipped_cash = 0

    # open_positions holds trades currently active in slots
    open_positions: list[dict] = []
    # settlement_queue holds (settle_date_str, cash_returned)
    settlement_queue: list[tuple[str, float]] = []
    taken_trades: list[dict] = []

    for day_str in all_calendar_days:
        # 1. Process settlements due on or before today
        remaining_settlements = []
        for s_date, cash_back in settlement_queue:
            if s_date <= day_str:
                cash += cash_back
            else:
                remaining_settlements.append((s_date, cash_back))
        settlement_queue = remaining_settlements

        # 2. Identify active slots: trades whose exit_date >= day_str.
        # Trades with exit_date < day_str have already exited and released their slot.
        active_slots = [p for p in open_positions if p["exit_date"] >= day_str]

        # 3. Process candidate entries triggering on this day
        candidates = trades_by_entry.get(day_str, [])
        if candidates:
            # Sort candidates by (-rs_rating, symbol)
            sorted_candidates = sorted(
                candidates,
                key=lambda c: (-(c.get("rs_rating_at_signal") or 0), c.get("symbol", ""))
            )

            for cand in sorted_candidates:
                if len(active_slots) >= 5:
                    skipped_slots += 1
                    continue

                entry_p = float(cand["entry_price"])
                atr = float(cand.get("atr_at_signal") or 0.0)
                stop_p = float(cand.get("stop_reference", entry_p * 0.94))
                risk_dist = 2.0 * atr if atr > 0 else abs(entry_p - stop_p)
                if risk_dist <= 0:
                    risk_dist = entry_p * 0.02

                qty_risk = math.floor(max_risk / risk_dist)
                qty_tranche = math.floor(min(max_tranche, cash) / entry_p)
                qty = min(qty_risk, qty_tranche)

                notional = qty * entry_p
                if qty < 1 or notional > cash:
                    skipped_cash += 1
                    continue

                gross_pct = float(cand.get("gross_return_pct", 0.0))
                # Statutory round-trip charges (cost_pct = 0.0030 -> 0.30%)
                net_pnl = round(notional * ((gross_pct - (cost_pct * 100.0)) / 100.0), 2)

                cash -= notional
                trades_taken += 1

                pos_record = {
                    "symbol": cand["symbol"],
                    "entry_date": cand["entry_date"],
                    "exit_date": cand["exit_date"],
                    "qty": qty,
                    "notional": notional,
                    "net_pnl": net_pnl,
                }
                open_positions.append(pos_record)
                active_slots.append(pos_record)
                taken_trades.append(pos_record)

        # 4. Handle end-of-day exits: positions exiting today (exit_date == day_str)
        # Move them to settlement_queue for T+1 settlement (exit_date + 1 calendar day)
        still_open = []
        for pos in open_positions:
            if pos["exit_date"] == day_str:
                exit_dt = datetime.fromisoformat(pos["exit_date"]).date()
                settle_dt = (exit_dt + timedelta(days=1)).isoformat()
                settlement_queue.append((settle_dt, pos["notional"] + pos["net_pnl"]))
            elif pos["exit_date"] > day_str:
                still_open.append(pos)
        open_positions = still_open

        # 5. Calculate current equity: cash + cost basis of open positions + pending settlement cash
        current_equity = (
            cash
            + sum(p["notional"] for p in open_positions)
            + sum(cb for _, cb in settlement_queue)
        )
        peak_equity = max(peak_equity, current_equity)
        dd = (peak_equity - current_equity) / peak_equity * 100.0 if peak_equity > 0 else 0.0
        max_dd = max(max_dd, dd)

    # 6. Final equity after all settlements process
    final_equity = (
        cash
        + sum(cb for _, cb in settlement_queue)
        + sum(p["notional"] + p["net_pnl"] for p in open_positions)
    )
    net_pnl = final_equity - starting_capital
    roi_pct = (net_pnl / starting_capital) * 100.0

    return {
        "starting_capital_inr": round(starting_capital, 2),
        "ending_capital_inr": round(final_equity, 2),
        "net_pnl_inr": round(net_pnl, 2),
        "roi_pct": round(roi_pct, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "trades_taken": trades_taken,
        "trades_skipped_slots": skipped_slots,
        "trades_skipped_cash": skipped_cash,
        "total_signals": len(trades),
    }


def compute_monthly_breakdown(trades: list[dict]) -> dict:
    """Compute monthly breakdown for trades."""
    if not trades:
        return {}
    by_month = {}
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


def run_test(name: str, test_id: str, start_d: date, end_d: date, out_dir: Path, cfg_overrides: dict | None = None) -> dict:
    enable_scan_cache()
    base_cfg = load_config()
    current_cfg = copy.deepcopy(base_cfg)
    if cfg_overrides:
        for path_keys, val in cfg_overrides.items():
            ref = current_cfg
            for k in path_keys[:-1]:
                ref = ref.setdefault(k, {})
            ref[path_keys[-1]] = val

    cli_mod.get_config = lambda c=current_cfg: c

    print(f"\n{'='*75}")
    print(f"RUNNING: [{test_id}] {name} ({start_d} to {end_d})")
    print(f"OUTPUT DIR: {out_dir}")
    print(f"{'='*75}", flush=True)

    start_t = datetime.now(timezone.utc)
    res = cli_mod.run_backtest(
        start_date=start_d,
        end_date=end_d,
        symbols=None,
        out_dir=out_dir,
    )
    dur = (datetime.now(timezone.utc) - start_t).total_seconds()

    report_path = out_dir / "report.json"
    with open(report_path) as f:
        rep_data = json.load(f)

    trades = rep_data.get("trades", [])
    monthly = compute_monthly_breakdown(trades)
    sim_5l = simulate_5slot_portfolio(trades)

    ov = res.get("overall", {})
    bench = rep_data.get("benchmark", {})
    wf = res.get("walk_forward", {})
    train = wf.get("train", {}) if isinstance(wf, dict) else {}
    oos = wf.get("out_of_sample", {}) if isinstance(wf, dict) else {}

    record = {
        "id": test_id,
        "name": name,
        "period": f"{start_d} to {end_d}",
        "config_hash": res.get("config_hash_sha256", ""),
        "duration_sec": round(dur, 1),
        "benchmark_return_pct": bench.get("total_return_pct"),
        "overall": {
            "trade_count": ov.get("trade_count", 0),
            "wins": ov.get("wins", 0),
            "losses": ov.get("losses", 0),
            "win_rate_pct": round(ov.get("win_rate_pct", 0.0), 1),
            "expectancy_net_pct": round(ov.get("expectancy_net_return_pct_per_trade", 0.0), 2),
            "expectancy_r": ov.get("expectancy_r"),
            "profit_factor": ov.get("profit_factor"),
            "max_dd_seq": round(ov.get("max_drawdown_pct_sequential_trade_equity", 0.0), 1),
        },
        "walk_forward": {
            "train_trades": train.get("trade_count"),
            "train_pf": train.get("profit_factor"),
            "train_wr": round(train.get("win_rate_pct", 0.0), 1) if train.get("win_rate_pct") is not None else None,
            "oos_trades": oos.get("trade_count"),
            "oos_pf": oos.get("profit_factor"),
            "oos_wr": round(oos.get("win_rate_pct", 0.0), 1) if oos.get("win_rate_pct") is not None else None,
        },
        "monthly": monthly,
        "portfolio_5l": sim_5l,
    }

    print(f"[{test_id}] Completed in {dur:.1f}s | Hash: {record['config_hash'][:8]} | Benchmark: {record['benchmark_return_pct']:.2f}%")
    print(f"Overall: Trades={record['overall']['trade_count']} | WR={record['overall']['win_rate_pct']}% | "
          f"ExpR={record['overall']['expectancy_r']} | PF={record['overall']['profit_factor']}")
    print(f"5-Slot 5L Sim: ROI={sim_5l['roi_pct']}% (Net: ₹{sim_5l['net_pnl_inr']:,.2f}) | "
          f"MaxDD={sim_5l['max_drawdown_pct']}% | Taken={sim_5l['trades_taken']} / Skipped(slots={sim_5l['trades_skipped_slots']}, cash={sim_5l['trades_skipped_cash']})")

    return record


def main():
    results = []

    # 1. P3-0: Baseline Reference Blind H1 (Regime OFF, RS OFF)
    p3_0 = run_test(
        name="Baseline Reference (Regime OFF, RS OFF)",
        test_id="P3-0_blind_h1_2026",
        start_d=date(2026, 1, 1),
        end_d=date(2026, 5, 31),
        out_dir=Path("reports/backtests/phase3a/P3-0_blind_h1_2026"),
        cfg_overrides={
            ("market_context", "mode"): "informational",
            ("guardrails", "rs_filter", "enabled"): False,
        },
    )
    results.append(p3_0)

    # 2. P3-5: Locked Production Blind H1 (Bear-Block + RS >= 50)
    p3_5_h1 = run_test(
        name="Production Strategy Blind H1 (Bear-Block + RS >= 50)",
        test_id="P3-5_blind_h1_2026",
        start_d=date(2026, 1, 1),
        end_d=date(2026, 5, 31),
        out_dir=Path("reports/backtests/phase3a/P3-5_blind_h1_2026"),
        cfg_overrides=None,
    )
    results.append(p3_5_h1)

    # 3. P3-5: Locked Production Full YTD (2026-01-01 to 2026-11-20)
    p3_5_ytd = run_test(
        name="Production Strategy Full YTD (Bear-Block + RS >= 50)",
        test_id="P3-5_ytd_2026",
        start_d=date(2026, 1, 1),
        end_d=date(2026, 11, 20),
        out_dir=Path("reports/backtests/phase3a/P3-5_ytd_2026"),
        cfg_overrides=None,
    )
    results.append(p3_5_ytd)

    # Save summary
    summary_path = Path("reports/backtests/phase3a/h1_and_ytd_verification_summary.json")
    with open(summary_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nSaved consolidated verification summary to {summary_path}")


if __name__ == "__main__":
    main()
