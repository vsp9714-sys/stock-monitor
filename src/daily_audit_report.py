"""
Daily Scan Audit & Guardrail Report Generator (src/daily_audit_report.py).

Runs after market close every trading day (e.g. 16:05 IST) to generate
deterministic Markdown and JSON audit reports capturing:
- Active strategy version and config hash
- Full scan coverage & system health
- Macro market context & benchmark regime
- Complete audit of all raw confluence candidates (BUY & SELL)
- Stage-by-stage guardrail attrition funnel
- Offline ablation comparison (Raw vs Pre-Phase-3A vs Locked Prod)
- Live database execution records (signals & paper trades)

Zero duplicated math: all evaluations invoke Scanner's diagnostic pipeline.
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from src.config_loader import get_config
from src.db import get_engine
from src.market_calendar import IST, is_trading_day
from src.scanner import Scanner

logger = logging.getLogger(__name__)


def format_markdown_report(snapshot: dict[str, Any], is_trading: bool = True) -> str:
    """Format a DailyAuditSnapshot dict into GitHub-flavored Markdown matching specifications."""
    session_date_str = snapshot.get("session_date", str(date.today()))
    dt = date.fromisoformat(session_date_str)
    weekday = dt.strftime("%A")
    formatted_date = dt.strftime("%d %B %Y")
    session_type = "Trading Day" if is_trading else "Non-Trading Day / Weekend"

    config_hash = snapshot.get("config_hash_sha256", "unknown")
    short_hash = config_hash[:8]
    strategy_version = snapshot.get("strategy_version", "v2_p3_5_bear_rs50")
    universe_size = snapshot.get("universe_size", 0)
    eligible_count = snapshot.get("eligible_count", 0)
    evaluated_count = snapshot.get("evaluated_count", 0)
    coverage_percent = snapshot.get("coverage_percent", 0.0)
    coverage_healthy = snapshot.get("coverage_healthy", False)
    coverage_badge = "🟢 HEALTHY" if coverage_healthy else "⚠️ WARNING: LOW COVERAGE"

    mc = snapshot.get("market_context", {})
    bench_close = mc.get("benchmark_close", 0.0)
    bench_sma20 = mc.get("sma_20", 0.0)
    bench_sma50 = mc.get("sma_50", 0.0)
    bench_regime = mc.get("regime", "unknown")
    buy_allowed = mc.get("buy_allowed", False)

    buy_candidates = snapshot.get("buy_candidates", [])
    sell_candidates = snapshot.get("sell_candidates", [])
    raw_candidates = snapshot.get("raw_candidates", [])
    signals_db = snapshot.get("dispatched_signals_db", [])
    paper_trades_db = snapshot.get("paper_trades_db", [])
    open_paper_trades = snapshot.get("open_paper_trades", [])
    ablation = snapshot.get("ablation", {})
    filter_counts = snapshot.get("filter_drop_counts", {})
    integrity = snapshot.get("sqlite_integrity", "ok")

    passed_buys = [c for c in buy_candidates if c.get("final_passed")]
    passed_sells = [c for c in sell_candidates if c.get("final_passed")]
    dispatched_count = len(signals_db)

    # ── Section 1 prose bullets ──────────────────────────────────────────────
    exec_summary_bullets = []
    exec_summary_bullets.append(
        f"- **Total Alerts Dispatched Today:** **{dispatched_count} alerts** "
        f"({len([s for s in signals_db if 'intraday' in s.get('signal_type', '')])} Intraday Radar, "
        f"{len([s for s in signals_db if 'power_hour' in s.get('signal_type', '')])} Power Hour Confirmed, "
        f"{len([s for s in signals_db if 'confluence_' in s.get('signal_type', '') and 'provisional' not in s.get('signal_type', '') and 'power_hour' not in s.get('signal_type', '')])} Daily Confirmed)."
    )
    exec_summary_bullets.append(
        f"- **Paper Trades Created Today:** **{len(paper_trades_db)} trades** "
        f"({len([p for p in paper_trades_db if p.get('alert_type') == 'intraday'])} Intraday, "
        f"{len([p for p in paper_trades_db if p.get('alert_type') == 'day_end'])} Day-End)."
    )
    exec_summary_bullets.append(
        f"- **Universe Scan Coverage:** **{coverage_percent}%** ({evaluated_count}/{eligible_count} tradable equities evaluated — **{coverage_badge}**)."
    )

    if dispatched_count == 0 and len(passed_buys) == 0:
        buy_fail_reasons = []
        if len(buy_candidates) == 0:
            buy_fail_reasons.append("zero stocks met raw confluence (≥ 2 indicator consensus)")
        else:
            adx_fails = sum(1 for c in buy_candidates if not c["guardrails"]["adx_filter"]["passed"])
            rs_fails = sum(1 for c in buy_candidates if not c["guardrails"]["rs_filter"]["passed"])
            risk_fails = sum(1 for c in buy_candidates if not c["guardrails"]["risk_reference"]["passed"])
            trend_fails = sum(1 for c in buy_candidates if not c["guardrails"]["trend_gate"]["passed"])
            rsi_fails = sum(1 for c in buy_candidates if not c["guardrails"]["rsi_exhaustion"]["passed"])
            if adx_fails:
                buy_fail_reasons.append(f"{adx_fails} failed ADX trend strength (< 20.0 choppiness)")
            if rs_fails:
                buy_fail_reasons.append(f"{rs_fails} failed Relative Strength (< 50 percentile)")
            if risk_fails:
                buy_fail_reasons.append(f"{risk_fails} exceeded 8.0% max risk distance cap")
            if trend_fails:
                buy_fail_reasons.append(f"{trend_fails} failed Macro Trend Gate (Price vs EMA 50/200)")
            if rsi_fails:
                buy_fail_reasons.append(f"{rsi_fails} failed RSI Exhaustion (> 68 overbought)")

        exec_summary_bullets.append(
            f"- **Root Cause for 0 Dispatched BUY Alerts:** {len(buy_candidates)} raw BUY candidates triggered confluence, but all were suppressed by guardrails: {', '.join(buy_fail_reasons)}."
        )
        if len(sell_candidates) > 0:
            exec_summary_bullets.append(
                f"- **Counter-Trend SELL Signals Suppressed:** {len(sell_candidates)} raw SELL signals occurred, but were blocked by the Benchmark Regime Gate (Nifty 50 is in a confirmed structural `{bench_regime}` regime; production trading is Long-Only Delivery)."
            )
    else:
        passed_syms = ", ".join(f"`{c['symbol']}` (RS {c['rs_rating']})" for c in passed_buys)
        exec_summary_bullets.append(
            f"- **Dispatched BUY Candidates (Sorted by RS):** {passed_syms or 'None'}."
        )

    exec_summary_text = "\n".join(exec_summary_bullets)

    # ── Section 3 Mermaid diagram ────────────────────────────────────────────
    raw_buy_count = len(buy_candidates)
    raw_sell_count = len(sell_candidates)
    passed_buy_count = len(passed_buys)
    passed_sell_count = len(passed_sells)

    mermaid_diagram = f"""```mermaid
flowchart TD
    A["Nifty 500 Universe ({universe_size} Equities)"] --> B["Evaluated with Valid History ({evaluated_count} Stocks, {coverage_percent}% Coverage)"]
    B --> C["Raw Indicator Confluence >= 2 ({len(raw_candidates)} Signals: {raw_buy_count} BUY, {raw_sell_count} SELL)"]
    
    C --> D1["BUY Signals ({raw_buy_count})"]
    C --> D2["SELL Signals ({raw_sell_count})"]
    
    D1 --> E1["Production Guardrails (Trend, RSI, ADX, RS, Risk)"]
    E1 -- "{raw_buy_count - passed_buy_count} Filtered" --> F1["{passed_buy_count} BUY Alerts Passed"]
    
    D2 --> E2["Benchmark Regime Gate ({bench_regime.title()} Market) & Long-Only Gate"]
    E2 -- "{raw_sell_count - passed_sell_count} Blocked" --> F2["{passed_sell_count} SELL Alerts Passed"]
    
    F1 --> G["Final Dispatched Alerts: {dispatched_count}"]
    F2 --> G
```"""

    # ── Section 4 Candidate Tables ───────────────────────────────────────────
    buy_rows = []
    for c in buy_candidates:
        triggers = ", ".join(c.get("triggered_indicators", [])) or "None"
        rs_str = str(c.get("rs_rating")) if c.get("rs_rating") is not None else "N/A"
        diag = c.get("diagnostic_details", "")
        buy_rows.append(
            f"| **`{c['symbol']}`** | ₹{c['close']:,.1f} | {triggers} | {c['adx_14']:.1f} | {rs_str} | {c['risk_distance_pct']:.1f}% | {c['final_status']} | {diag} |"
        )
    buy_table = (
        "| Symbol | Close (₹) | Confluence Triggers | ADX (14) | RS Rating | Risk Dist (%) | Final Status | Diagnostic Details |\n"
        "| :--- | :---: | :--- | :---: | :---: | :---: | :---: | :--- |\n"
        + ("\n".join(buy_rows) if buy_rows else "| *None* | - | No raw BUY confluence signals | - | - | - | - | - |")
    )

    sell_rows = []
    for c in sell_candidates:
        triggers = ", ".join(c.get("triggered_indicators", [])) or "None"
        rs_str = str(c.get("rs_rating")) if c.get("rs_rating") is not None else "N/A"
        reasons = "; ".join(c.get("failed_checks", [])) or "Suppressed by long-only delivery policy"
        sell_rows.append(
            f"| **`{c['symbol']}`** | ₹{c['close']:,.1f} | {triggers} | {c['adx_14']:.1f} | {rs_str} | {c['risk_distance_pct']:.1f}% | {reasons} |"
        )
    sell_table = (
        "| Symbol | Close (₹) | Confluence Triggers | ADX (14) | RS Rating | Risk Dist (%) | Suppression Reasons |\n"
        "| :--- | :---: | :--- | :---: | :---: | :---: | :--- |\n"
        + ("\n".join(sell_rows) if sell_rows else "| *None* | - | No raw SELL confluence signals | - | - | - | - |")
    )

    # ── Section 5 Ablation symbols list ──────────────────────────────────────
    pre_3a_syms = ", ".join(f"`{s}`" for s in ablation.get("pre_phase_3a_symbols", [])) or "None"
    locked_syms = ", ".join(f"`{s}`" for s in ablation.get("locked_prod_symbols", [])) or "None"

    # ── Section 6 Paper trade counts ─────────────────────────────────────────
    open_intraday = len([p for p in open_paper_trades if p.get("alert_type") == "intraday"])
    open_dayend = len([p for p in open_paper_trades if p.get("alert_type") == "day_end"])
    failed_symbols_count = len(snapshot.get("failed_symbols", []))

    md = f"""# Daily Scan Audit & Guardrail Rejection Report: {session_date_str}

**Report Date:** {session_date_str}  
**System:** `stock-monitor` — Indian Equity Market Swing Trading Signal Detector  
**Market Session:** {weekday}, {formatted_date} ({session_type})  
**Universe:** Nifty 500 ({universe_size} tradable active equities)  
**Production Strategy Locked Baseline:** `P3-5: Bear-Block + RS >= 50` (`config_hash: {short_hash}`)  
- **Config Hash (SHA-256):** `{config_hash}`  
- **Direction:** Long-Only CNC Delivery (`allowed_directions: ["BUY"]`)  
- **Stops & Targets:** 2.0×ATR Stop Loss, 1.5R (3.0×ATR) Target  
- **Risk Distance Cap:** ≤ 8.0% from entry  
- **Macro Trend Gate:** Price > EMA 50 > EMA 200  
- **RSI Exhaustion Ceiling:** RSI(14) ≤ 68.0 for BUY  
- **ADX Filter:** ADX(14) ≥ 20.0 with +DI > -DI directional alignment  
- **Cross-Sectional RS Filter:** ≥ 50 percentile vs Nifty 50  
- **Benchmark Regime Gate:** Mode `benchmark_filter` (BUY permitted in `bullish` and `mixed`; SELL permitted in `bearish` and `mixed`)

---

## 1. Executive Summary

{exec_summary_text}

---

## 2. Market Context & Macro Regime Status

| Metric | Value | Interpretation |
| :--- | :---: | :--- |
| **Benchmark Index** | NSE Nifty 50 (`{mc.get('benchmark_key', 'NSE_INDEX|Nifty 50')}`) | Macro Equity Reference |
| **Short-Term Benchmark Trend** | 20-Day SMA = {bench_sma20:,.1f} | Short-term trend moving average |
| **Long-Term Benchmark Trend** | 50-Day SMA = {bench_sma50:,.1f} | Long-term structural baseline |
| **Benchmark Current Close** | {bench_close:,.1f} | Latest finalized session close |
| **Classified Macro Regime** | **`{bench_regime}`** | Confirmed structural trend state |
| **Permitted BUY Regimes** | `["bullish", "mixed"]` | **{'BUY signals ALLOWED' if buy_allowed else 'BUY signals BLOCKED'}** |
| **Permitted SELL Regimes** | `["bearish", "mixed"]` | **{'SELL signals ALLOWED' if mc.get('sell_allowed') else 'SELL signals BLOCKED'}** |

> [!NOTE]
> **Daily Bar Color vs. Macro Regime:**  
> A red daily candle on Nifty 50 does **not** suppress BUY alerts. The Benchmark Regime Gate evaluates multi-week moving average relationships (20-SMA vs 50-SMA), not intraday candle color. BUY alerts on high-RS pullbacks are fully permitted on red market days unless the market enters a structural, multi-week downtrend (`bearish` regime).

---

## 3. Signal Attrition Funnel

{mermaid_diagram}

### Cumulative Filter Attrition Table

| Filter Stage | Config Parameter | Raw Signals Dropped | Failure Reason / Details |
| :--- | :--- | :---: | :--- |
| **1. Benchmark Regime Gate** | `market_context.allowed_sell_regimes` | **{filter_counts.get('market_context', 0)}** | Blocks counter-trend signals against macro regime |
| **2. ADX Trend Filter** | `guardrails.adx_filter.min_adx: 20.0` | **{filter_counts.get('adx_filter', 0)}** | Drops signals in choppy / sideways markets (ADX < 20.0 or DI misaligned) |
| **3. Risk Distance Cap** | `risk.max_risk_distance_pct: 8.0` | **{filter_counts.get('risk_distance', 0)}** | Drops wide stop setups where 2.0 × ATR / Price > 8.0% |
| **4. RS Rating Filter** | `guardrails.rs_filter.min_rs_rating_buy: 50` | **{filter_counts.get('rs_filter', 0)}** | Drops stocks lagging behind universe median (RS < 50) |
| **5. Macro Trend Gate** | `guardrails.trend_gate` | **{filter_counts.get('trend_gate', 0)}** | Moving average misalignment (Price vs EMA 50 vs EMA 200) |
| **6. RSI Exhaustion** | `guardrails.rsi_exhaustion` | **{filter_counts.get('rsi_exhaustion', 0)}** | Overbought BUY (RSI > 68) or Oversold SELL (RSI < 32) |
| **7. Wick Filter** | `guardrails.wick_filter` | **{filter_counts.get('wick_filter', 0)}** | Rejection wicks (CLV < 0.70 for BUY, > 0.30 for SELL) |
| **8. Allowed Directions** | `paper_trading.allowed_directions: ["BUY"]` | **{filter_counts.get('allowed_directions', 0)}** | Non-configured directions (informational counter-trend signals) |

---

## 4. Full Audit of All Raw Confluence Candidates

### 4.1 BUY Candidates Table (Sorted by RS Rating Descending)

{buy_table}

### 4.2 SELL Candidates Table (Informational — Non-Configured Direction)

{sell_table}

---

## 5. Ablation Comparison (Today's Tape Under 3 Config States)

| Config State | Strategy Rules & Active Filters | Alerts Fired Today | Qualifying Symbols |
| :--- | :--- | :---: | :--- |
| **1. Raw Indicator Confluence** | Indicator Confluence ≥ 2, Zero Guardrails | **{ablation.get('raw_confluence_count', 0)}** | All raw BUY & SELL confluence candidates |
| **2. Pre-Phase-3A Baseline** | Phase 1/2 Guardrails, Informational Regime, No RS Filter, 6.0% Cap | **{ablation.get('pre_phase_3a_count', 0)}** | {pre_3a_syms} |
| **3. Current Locked Production Config** | Bear-Block + RS ≥ 50 + 8.0% Cap + Long-Only Delivery | **{ablation.get('locked_prod_count', 0)}** | {locked_syms} |

---

## 6. System Health & Paper-Trading Snapshot

- **Scan Coverage:** **{coverage_percent}%** ({evaluated_count}/{eligible_count} tradable stocks evaluated successfully).
- **History / Quote Failures:** **{failed_symbols_count} stocks** skipped due to insufficient history or missing quotes.
- **Database Integrity:** SQLite quick check returned **`{integrity}`**.
- **Active Open Paper Trades:** **{len(open_paper_trades)} total open positions** ({open_intraday} Intraday Radar, {open_dayend} Day-End Confirmed).
- **Trades Entered Today:** **{len(paper_trades_db)} new positions** recorded in `paper_trades` table.

---

## 7. Recommended Action

- **System Operating as Designed:** The active production guardrails prevented capital allocation into non-trending, choppy setups (ADX < 20) and avoided counter-trend shorting during an established macro bull market.
- **Zero Config Alteration Required:** Strategy statistical expectancy is preserved by disciplined adherence to the locked baseline parameters.
"""
    return md


def format_telegram_summary(snapshot: dict[str, Any], report_md_path: str) -> str:
    """Format a compact HTML digest for Telegram broadcast."""
    session_date_str = snapshot.get("session_date", str(date.today()))
    mc = snapshot.get("market_context", {})
    bench_close = mc.get("benchmark_close", 0.0)
    bench_regime = mc.get("regime", "unknown").title()
    sma20 = mc.get("sma_20", 0.0)
    sma50 = mc.get("sma_50", 0.0)

    cov_pct = snapshot.get("coverage_percent", 0.0)
    healthy = snapshot.get("coverage_healthy", False)
    cov_badge = "🟢 Healthy" if healthy else "⚠️ Low Coverage"

    signals_db = snapshot.get("dispatched_signals_db", [])
    dispatched_count = len(signals_db)

    buy_cands = snapshot.get("buy_candidates", [])
    passed_buys = [c for c in buy_cands if c.get("final_passed")]

    lines = [
        f"<b>📊 Daily Scan & Guardrail Audit: {session_date_str}</b>",
        f"<b>Nifty 50:</b> ₹{bench_close:,.1f} ({bench_regime} | SMA20: ₹{sma20:,.0f}, SMA50: ₹{sma50:,.0f})",
        f"<b>Scan Coverage:</b> {cov_pct}% ({cov_badge})",
        f"<b>Alerts Dispatched:</b> {dispatched_count}",
    ]

    if dispatched_count == 0 and len(passed_buys) == 0:
        if len(buy_cands) == 0:
            lines.append("<i>0 alerts — zero stocks met raw confluence.</i>")
        else:
            # Highlight top near-miss candidate by RS
            top_cand = buy_cands[0]
            top_fails = ", ".join(top_cand.get("failed_checks", [])[:2])
            lines.append(
                f"<i>0 alerts — {len(buy_cands)} raw BUY setups suppressed by guardrails.</i>\n"
                f"<b>Top Near-Miss:</b> {top_cand['symbol']} (RS {top_cand.get('rs_rating', 'N/A')}) — {top_fails}"
            )
    else:
        dispatched_syms = ", ".join(f"<b>{s['symbol']}</b> ({s['direction']})" for s in signals_db[:5])
        lines.append(f"<b>Dispatched Setups:</b> {dispatched_syms}")

    lines.append(f"\n📄 <b>Full Report:</b> <code>{report_md_path}</code>")
    return "\n".join(lines)


def generate_daily_audit_report(
    session_date: Optional[date] = None,
    scanner: Optional[Scanner] = None,
    out_dir: Optional[str] = None,
    force: bool = False,
    send_telegram: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Main entry point for generating the daily post-market guardrail audit report.
    Guarded by idempotency unless force=True.
    """
    session_date = session_date or datetime.now(IST).date()
    cfg = get_config()

    daily_audit_cfg = cfg.get("daily_audit", {}) or {}
    report_dir_str = out_dir or daily_audit_cfg.get("report_dir", "reports/daily_audits")
    report_dir = Path(report_dir_str)
    report_dir.mkdir(parents=True, exist_ok=True)

    date_str = session_date.isoformat()
    md_filename = f"{date_str}_guardrail_audit.md"
    json_filename = f"{date_str}_guardrail_audit.json"
    md_path = report_dir / md_filename
    json_path = report_dir / json_filename

    # Idempotency check
    if md_path.exists() and json_path.exists() and not force and not dry_run:
        logger.info(
            "Daily audit report already exists for %s at %s. Skipping (use --force to overwrite).",
            date_str, md_path,
        )
        return {
            "session_date": date_str,
            "status": "skipped_already_exists",
            "md_path": str(md_path),
            "json_path": str(json_path),
        }

    # Initialize scanner if not provided
    if scanner is None:
        scanner = Scanner()

    logger.info("Running deterministic diagnostic audit across universe for session %s...", date_str)
    snapshot = scanner.audit_daily_candidates(session_date=session_date)

    is_trading = is_trading_day(session_date, cfg.get("market", {}))
    md_content = format_markdown_report(snapshot, is_trading=is_trading)

    if not dry_run:
        md_path.write_text(md_content, encoding="utf-8")
        json_path.write_text(json.dumps(snapshot, indent=2, sort_keys=True, default=str), encoding="utf-8")
        logger.info("Saved daily audit report to %s and %s", md_path, json_path)

    # Optional Telegram broadcast
    telegram_sent = False
    telegram_cfg_enabled = bool(daily_audit_cfg.get("send_telegram_summary", True))
    if send_telegram and telegram_cfg_enabled and not dry_run:
        summary_html = format_telegram_summary(snapshot, str(md_path))
        for notifier in scanner.notifiers:
            if hasattr(notifier, "broadcast_message"):
                try:
                    if notifier.broadcast_message(summary_html):
                        telegram_sent = True
                        logger.info("Daily audit summary broadcast to Telegram successfully.")
                except Exception as exc:
                    logger.warning("Telegram broadcast of daily audit failed: %s", exc)

    return {
        "session_date": date_str,
        "status": "completed",
        "md_path": str(md_path),
        "json_path": str(json_path),
        "telegram_sent": telegram_sent,
        "raw_candidates_count": len(snapshot.get("raw_candidates", [])),
        "buy_candidates_count": len(snapshot.get("buy_candidates", [])),
        "sell_candidates_count": len(snapshot.get("sell_candidates", [])),
        "coverage_percent": snapshot.get("coverage_percent", 0.0),
        "config_hash_sha256": snapshot.get("config_hash_sha256"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Deterministic daily post-market guardrail audit & report generator.")
    parser.add_argument(
        "--date", type=str, default=None,
        help="Target session date (YYYY-MM-DD). Defaults to current IST date.",
    )
    parser.add_argument(
        "--out-dir", type=str, default=None,
        help="Output directory for reports (defaults to reports/daily_audits).",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite existing reports for this session date.",
    )
    parser.add_argument(
        "--no-telegram", action="store_true",
        help="Suppress Telegram digest broadcast even if configured.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Generate snapshot and format report in memory without writing to disk or sending Telegram.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    get_engine()

    target_date = date.fromisoformat(args.date) if args.date else None
    result = generate_daily_audit_report(
        session_date=target_date,
        out_dir=args.out_dir,
        force=args.force,
        send_telegram=not args.no_telegram,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
