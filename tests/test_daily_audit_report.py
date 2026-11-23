"""
Unit tests for Daily Scan Audit & Guardrail Report Generator (src/daily_audit_report.py).
"""
import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.daily_audit_report import (
    format_markdown_report,
    format_telegram_summary,
    generate_daily_audit_report,
)


@pytest.fixture
def mock_zero_alert_snapshot():
    return {
        "session_date": "2026-11-23",
        "strategy_version": "v2_p3_5_bear_rs50",
        "config_hash_sha256": "0d5ad15af0a1af4e8f2921e895c8f92374e270753bca65cbd6b37414de6a4526",
        "universe_size": 495,
        "eligible_count": 495,
        "evaluated_count": 487,
        "coverage_ratio": 0.9838,
        "coverage_percent": 98.38,
        "coverage_healthy": True,
        "failed_symbols": ["SYM1", "SYM2"],
        "market_context": {
            "benchmark_key": "NSE_INDEX|Nifty 50",
            "benchmark_close": 25800.0,
            "sma_20": 24396.0,
            "sma_50": 23512.0,
            "regime": "bullish",
            "allowed_buy_regimes": ["bullish", "mixed"],
            "allowed_sell_regimes": ["bearish", "mixed"],
            "buy_allowed": True,
            "sell_allowed": False,
        },
        "raw_candidates": [
            {
                "symbol": "JSWINFRA",
                "company_name": "JSW Infrastructure Ltd",
                "direction": "BUY",
                "confluence_score": 2,
                "total_signals": 3,
                "triggered_indicators": ["RSI (14)", "Volume Breakout"],
                "close": 353.6,
                "adx_14": 14.2,
                "rs_rating": 74,
                "risk_distance_pct": 6.1,
                "final_status": "❌ REJECTED",
                "final_passed": False,
                "failed_checks": ["ADX Filter (ADX 14.2 < 20.0 or DI misaligned)"],
                "diagnostic_details": "❌ REJECTED: ADX Filter (ADX 14.2 < 20.0 or DI misaligned)",
                "guardrails": {
                    "allowed_directions": {"passed": True},
                    "trend_gate": {"passed": True},
                    "rsi_exhaustion": {"passed": True},
                    "adx_filter": {"passed": False},
                    "wick_filter": {"passed": True},
                    "market_context": {"passed": True},
                    "rs_filter": {"passed": True},
                    "risk_reference": {"passed": True},
                },
            },
            {
                "symbol": "TATASTEEL",
                "company_name": "Tata Steel Ltd",
                "direction": "SELL",
                "confluence_score": 3,
                "total_signals": 3,
                "triggered_indicators": ["RSI (14)", "EMA 9×21", "Volume Breakout"],
                "close": 163.0,
                "adx_14": 23.4,
                "rs_rating": 35,
                "risk_distance_pct": 5.2,
                "final_status": "❌ REJECTED",
                "final_passed": False,
                "failed_checks": ["Benchmark Regime Gate (Nifty is bullish)"],
                "diagnostic_details": "❌ REJECTED: Benchmark Regime Gate (Nifty is bullish)",
                "guardrails": {
                    "allowed_directions": {"passed": False},
                    "trend_gate": {"passed": True},
                    "rsi_exhaustion": {"passed": True},
                    "adx_filter": {"passed": True},
                    "wick_filter": {"passed": True},
                    "market_context": {"passed": False},
                    "rs_filter": {"passed": True},
                    "risk_reference": {"passed": True},
                },
            },
        ],
        "buy_candidates": [
            {
                "symbol": "JSWINFRA",
                "company_name": "JSW Infrastructure Ltd",
                "direction": "BUY",
                "confluence_score": 2,
                "total_signals": 3,
                "triggered_indicators": ["RSI (14)", "Volume Breakout"],
                "close": 353.6,
                "adx_14": 14.2,
                "rs_rating": 74,
                "risk_distance_pct": 6.1,
                "final_status": "❌ REJECTED",
                "final_passed": False,
                "failed_checks": ["ADX Filter (ADX 14.2 < 20.0 or DI misaligned)"],
                "diagnostic_details": "❌ REJECTED: ADX Filter (ADX 14.2 < 20.0 or DI misaligned)",
                "guardrails": {
                    "allowed_directions": {"passed": True},
                    "trend_gate": {"passed": True},
                    "rsi_exhaustion": {"passed": True},
                    "adx_filter": {"passed": False},
                    "wick_filter": {"passed": True},
                    "market_context": {"passed": True},
                    "rs_filter": {"passed": True},
                    "risk_reference": {"passed": True},
                },
            }
        ],
        "sell_candidates": [
            {
                "symbol": "TATASTEEL",
                "company_name": "Tata Steel Ltd",
                "direction": "SELL",
                "confluence_score": 3,
                "total_signals": 3,
                "triggered_indicators": ["RSI (14)", "EMA 9×21", "Volume Breakout"],
                "close": 163.0,
                "adx_14": 23.4,
                "rs_rating": 35,
                "risk_distance_pct": 5.2,
                "final_status": "❌ REJECTED",
                "final_passed": False,
                "failed_checks": ["Benchmark Regime Gate (Nifty is bullish)"],
                "diagnostic_details": "❌ REJECTED: Benchmark Regime Gate (Nifty is bullish)",
                "guardrails": {
                    "allowed_directions": {"passed": False},
                    "trend_gate": {"passed": True},
                    "rsi_exhaustion": {"passed": True},
                    "adx_filter": {"passed": True},
                    "wick_filter": {"passed": True},
                    "market_context": {"passed": False},
                    "rs_filter": {"passed": True},
                    "risk_reference": {"passed": True},
                },
            }
        ],
        "filter_drop_counts": {
            "adx_filter": 1,
            "market_context": 1,
            "allowed_directions": 1,
            "trend_gate": 0,
            "rsi_exhaustion": 0,
            "wick_filter": 0,
            "rs_filter": 0,
            "risk_distance": 0,
        },
        "dispatched_signals_db": [],
        "paper_trades_db": [],
        "open_paper_trades": [{"symbol": "ABC", "alert_type": "day_end"}],
        "sqlite_integrity": "ok",
        "ablation": {
            "raw_confluence_count": 2,
            "pre_phase_3a_count": 1,
            "pre_phase_3a_symbols": ["TATASTEEL"],
            "locked_prod_count": 0,
            "locked_prod_symbols": [],
        },
    }


@pytest.fixture
def mock_multi_alert_snapshot():
    return {
        "session_date": "2026-11-24",
        "strategy_version": "v2_p3_5_bear_rs50",
        "config_hash_sha256": "0d5ad15af0a1af4e8f2921e895c8f92374e270753bca65cbd6b37414de6a4526",
        "universe_size": 495,
        "eligible_count": 495,
        "evaluated_count": 488,
        "coverage_ratio": 0.9858,
        "coverage_percent": 98.58,
        "coverage_healthy": True,
        "failed_symbols": ["SYM1"],
        "market_context": {
            "benchmark_key": "NSE_INDEX|Nifty 50",
            "benchmark_close": 25950.0,
            "sma_20": 24450.0,
            "sma_50": 23550.0,
            "regime": "bullish",
            "allowed_buy_regimes": ["bullish", "mixed"],
            "allowed_sell_regimes": ["bearish", "mixed"],
            "buy_allowed": True,
            "sell_allowed": False,
        },
        "raw_candidates": [
            {
                "symbol": "TCS",
                "company_name": "Tata Consultancy Services",
                "direction": "BUY",
                "confluence_score": 2,
                "total_signals": 3,
                "triggered_indicators": ["EMA 9×21", "MACD (12,26,9)"],
                "close": 4200.0,
                "adx_14": 26.5,
                "rs_rating": 85,
                "risk_distance_pct": 4.5,
                "final_status": "✅ PASSED / DISPATCHED",
                "final_passed": True,
                "failed_checks": [],
                "diagnostic_details": "✅ PASSED / DISPATCHED: Stop ₹4011.00 | Target ₹4483.50 | Qty 26 (₹4,914 Risk)",
                "guardrails": {
                    "allowed_directions": {"passed": True},
                    "trend_gate": {"passed": True},
                    "rsi_exhaustion": {"passed": True},
                    "adx_filter": {"passed": True},
                    "wick_filter": {"passed": True},
                    "market_context": {"passed": True},
                    "rs_filter": {"passed": True},
                    "risk_reference": {"passed": True},
                },
            },
            {
                "symbol": "INFY",
                "company_name": "Infosys Ltd",
                "direction": "BUY",
                "confluence_score": 2,
                "total_signals": 3,
                "triggered_indicators": ["EMA 9×21", "Volume Breakout"],
                "close": 1850.0,
                "adx_14": 24.1,
                "rs_rating": 72,
                "risk_distance_pct": 5.2,
                "final_status": "✅ PASSED / DISPATCHED",
                "final_passed": True,
                "failed_checks": [],
                "diagnostic_details": "✅ PASSED / DISPATCHED: Stop ₹1753.80 | Target ₹1994.30 | Qty 52 (₹5,002 Risk)",
                "guardrails": {
                    "allowed_directions": {"passed": True},
                    "trend_gate": {"passed": True},
                    "rsi_exhaustion": {"passed": True},
                    "adx_filter": {"passed": True},
                    "wick_filter": {"passed": True},
                    "market_context": {"passed": True},
                    "rs_filter": {"passed": True},
                    "risk_reference": {"passed": True},
                },
            },
        ],
        "buy_candidates": [
            {
                "symbol": "TCS",
                "company_name": "Tata Consultancy Services",
                "direction": "BUY",
                "confluence_score": 2,
                "total_signals": 3,
                "triggered_indicators": ["EMA 9×21", "MACD (12,26,9)"],
                "close": 4200.0,
                "adx_14": 26.5,
                "rs_rating": 85,
                "risk_distance_pct": 4.5,
                "final_status": "✅ PASSED / DISPATCHED",
                "final_passed": True,
                "failed_checks": [],
                "diagnostic_details": "✅ PASSED / DISPATCHED: Stop ₹4011.00 | Target ₹4483.50 | Qty 26 (₹4,914 Risk)",
                "guardrails": {
                    "allowed_directions": {"passed": True},
                    "trend_gate": {"passed": True},
                    "rsi_exhaustion": {"passed": True},
                    "adx_filter": {"passed": True},
                    "wick_filter": {"passed": True},
                    "market_context": {"passed": True},
                    "rs_filter": {"passed": True},
                    "risk_reference": {"passed": True},
                },
            },
            {
                "symbol": "INFY",
                "company_name": "Infosys Ltd",
                "direction": "BUY",
                "confluence_score": 2,
                "total_signals": 3,
                "triggered_indicators": ["EMA 9×21", "Volume Breakout"],
                "close": 1850.0,
                "adx_14": 24.1,
                "rs_rating": 72,
                "risk_distance_pct": 5.2,
                "final_status": "✅ PASSED / DISPATCHED",
                "final_passed": True,
                "failed_checks": [],
                "diagnostic_details": "✅ PASSED / DISPATCHED: Stop ₹1753.80 | Target ₹1994.30 | Qty 52 (₹5,002 Risk)",
                "guardrails": {
                    "allowed_directions": {"passed": True},
                    "trend_gate": {"passed": True},
                    "rsi_exhaustion": {"passed": True},
                    "adx_filter": {"passed": True},
                    "wick_filter": {"passed": True},
                    "market_context": {"passed": True},
                    "rs_filter": {"passed": True},
                    "risk_reference": {"passed": True},
                },
            },
        ],
        "sell_candidates": [],
        "filter_drop_counts": {
            "adx_filter": 0,
            "market_context": 0,
            "allowed_directions": 0,
            "trend_gate": 0,
            "rsi_exhaustion": 0,
            "wick_filter": 0,
            "rs_filter": 0,
            "risk_distance": 0,
        },
        "dispatched_signals_db": [
            {"symbol": "TCS", "signal_type": "confluence_buy", "direction": "BUY", "price": 4200.0, "confluence_score": 2},
            {"symbol": "INFY", "signal_type": "confluence_buy", "direction": "BUY", "price": 1850.0, "confluence_score": 2},
        ],
        "paper_trades_db": [
            {"symbol": "TCS", "direction": "BUY", "entry_price": 4200.0, "alert_type": "day_end"},
            {"symbol": "INFY", "direction": "BUY", "entry_price": 1850.0, "alert_type": "day_end"},
        ],
        "open_paper_trades": [
            {"symbol": "TCS", "alert_type": "day_end"},
            {"symbol": "INFY", "alert_type": "day_end"},
        ],
        "sqlite_integrity": "ok",
        "ablation": {
            "raw_confluence_count": 2,
            "pre_phase_3a_count": 2,
            "pre_phase_3a_symbols": ["INFY", "TCS"],
            "locked_prod_count": 2,
            "locked_prod_symbols": ["INFY", "TCS"],
        },
    }


def test_markdown_zero_alert_day(mock_zero_alert_snapshot):
    md = format_markdown_report(mock_zero_alert_snapshot, is_trading=True)
    assert "# Daily Scan Audit & Guardrail Rejection Report: 2026-11-23" in md
    assert "0d5ad15a" in md
    assert "**Total Alerts Dispatched Today:** **0 alerts**" in md
    assert "Nifty 500 Universe (495 Equities)" in md
    assert "```mermaid" in md
    assert "| **`JSWINFRA`** | ₹353.6 |" in md
    assert "| **`TATASTEEL`** | ₹163.0 |" in md
    assert "Pre-Phase-3A Baseline" in md
    assert "`TATASTEEL`" in md
    assert "A red daily candle on Nifty 50 does **not** suppress BUY alerts" in md


def test_markdown_multi_alert_day(mock_multi_alert_snapshot):
    md = format_markdown_report(mock_multi_alert_snapshot, is_trading=True)
    assert "# Daily Scan Audit & Guardrail Rejection Report: 2026-11-24" in md
    assert "**Total Alerts Dispatched Today:** **2 alerts**" in md
    assert "`TCS` (RS 85)" in md
    assert "`INFY` (RS 72)" in md
    # Check RS ordering: TCS (85) appears before INFY (72)
    pos_tcs = md.index("`TCS`")
    pos_infy = md.index("`INFY`")
    assert pos_tcs < pos_infy
    assert "✅ PASSED / DISPATCHED: Stop ₹4011.00" in md


def test_low_coverage_warning(mock_zero_alert_snapshot):
    snapshot = dict(mock_zero_alert_snapshot)
    snapshot["coverage_ratio"] = 0.85
    snapshot["coverage_percent"] = 85.0
    snapshot["coverage_healthy"] = False
    md = format_markdown_report(snapshot, is_trading=True)
    assert "⚠️ WARNING: LOW COVERAGE" in md


def test_telegram_summary_formatting(mock_zero_alert_snapshot, mock_multi_alert_snapshot):
    tg_zero = format_telegram_summary(mock_zero_alert_snapshot, "reports/daily_audits/2026-11-23_guardrail_audit.md")
    assert "<b>📊 Daily Scan & Guardrail Audit: 2026-11-23</b>" in tg_zero
    assert "<b>Nifty 50:</b> ₹25,800.0 (Bullish" in tg_zero
    assert "<b>Alerts Dispatched:</b> 0" in tg_zero
    assert "Top Near-Miss:</b> JSWINFRA (RS 74)" in tg_zero
    assert "<code>reports/daily_audits/2026-11-23_guardrail_audit.md</code>" in tg_zero

    tg_multi = format_telegram_summary(mock_multi_alert_snapshot, "reports/daily_audits/2026-11-24_guardrail_audit.md")
    assert "<b>Alerts Dispatched:</b> 2" in tg_multi
    assert "<b>TCS</b> (BUY)" in tg_multi


def test_idempotency_and_force(tmp_path, mock_zero_alert_snapshot):
    scanner = MagicMock()
    scanner.audit_daily_candidates.return_value = mock_zero_alert_snapshot
    scanner.notifiers = []

    # First run: writes files
    res1 = generate_daily_audit_report(
        session_date=date(2026, 11, 23),
        scanner=scanner,
        out_dir=str(tmp_path),
        force=False,
        send_telegram=False,
        dry_run=False,
    )
    assert res1["status"] == "completed"
    md_file = tmp_path / "2026-11-23_guardrail_audit.md"
    json_file = tmp_path / "2026-11-23_guardrail_audit.json"
    assert md_file.exists()
    assert json_file.exists()

    # Second run without force: skips
    scanner.audit_daily_candidates.reset_mock()
    res2 = generate_daily_audit_report(
        session_date=date(2026, 11, 23),
        scanner=scanner,
        out_dir=str(tmp_path),
        force=False,
        send_telegram=False,
        dry_run=False,
    )
    assert res2["status"] == "skipped_already_exists"
    scanner.audit_daily_candidates.assert_not_called()

    # Third run with force: overwrites
    res3 = generate_daily_audit_report(
        session_date=date(2026, 11, 23),
        scanner=scanner,
        out_dir=str(tmp_path),
        force=True,
        send_telegram=False,
        dry_run=False,
    )
    assert res3["status"] == "completed"
    scanner.audit_daily_candidates.assert_called_once()
