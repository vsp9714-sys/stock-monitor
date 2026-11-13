from datetime import date, datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import pandas as pd
import pytest

from src.notifiers.base import AlertPayload
from src.notifiers.telegram import TelegramNotifier
from src.scanner import Scanner
from src.signals.volume_breakout import (
    VolumeBreakoutResult,
    detect_volume_breakout,
    nse_expected_volume_fraction,
)


IST = ZoneInfo("Asia/Kolkata")


def test_nse_expected_volume_fraction_u_curve():
    # Pre-market: 09:00 IST -> returns min floor (0.05)
    t_pre = datetime(2026, 11, 13, 9, 0, tzinfo=IST)
    assert nse_expected_volume_fraction(t_pre) == 0.05

    # Opening Discovery end: 10:30 IST (75 mins elapsed) -> exactly 0.30 (30%)
    t_1030 = datetime(2026, 11, 13, 10, 30, tzinfo=IST)
    assert pytest.approx(nse_expected_volume_fraction(t_1030), rel=1e-3) == 0.30

    # Midday lull: 12:00 IST (165 mins elapsed, 90 mins into 210m mid phase) -> 0.30 + (90/210)*0.40 = 0.4714
    t_1200 = datetime(2026, 11, 13, 12, 0, tzinfo=IST)
    assert pytest.approx(nse_expected_volume_fraction(t_1200), rel=1e-3) == 0.4714

    # End of Midday / Power Hour start: 14:00 IST (285 mins elapsed) -> exactly 0.70 (70%)
    t_1400 = datetime(2026, 11, 13, 14, 0, tzinfo=IST)
    assert pytest.approx(nse_expected_volume_fraction(t_1400), rel=1e-3) == 0.70

    # Confirmation Window: 15:15 IST (360 mins elapsed, 75 mins into 90m power hour) -> 0.70 + (75/90)*0.30 = 0.95
    t_1515 = datetime(2026, 11, 13, 15, 15, tzinfo=IST)
    assert pytest.approx(nse_expected_volume_fraction(t_1515), rel=1e-3) == 0.95

    # Market close: 15:30 IST -> exactly 1.00 (100%)
    t_close = datetime(2026, 11, 13, 15, 30, tzinfo=IST)
    assert nse_expected_volume_fraction(t_close) == 1.00


def test_volume_breakout_pace_projection():
    # 25 reference candles with average volume 1,000
    ref_rows = [{"open": 100, "high": 105, "low": 95, "close": 100, "volume": 1000} for _ in range(25)]

    # Today: at 10:30 IST, cumulative volume is 600.
    # Unadjusted ratio is 600 / 1000 = 0.6x (which would fail 2.0x threshold).
    # But expected fraction at 10:30 IST is 0.30.
    # Pace is 600 / (0.30 * 1000) = 2.0x!
    today_row = {"open": 100, "high": 110, "low": 98, "close": 108, "volume": 600}
    df = pd.DataFrame(ref_rows + [today_row])

    # Without pace (expected_fraction=None): unadjusted ratio 0.6x fails
    res_unadjusted = detect_volume_breakout("TEST", df, volume_multiplier=2.0)
    assert res_unadjusted.triggered is False
    assert res_unadjusted.volume_ratio == 0.6

    # With pace at 10:30 IST: pace 2.0x triggers BUY breakout
    res_paced = detect_volume_breakout("TEST", df, volume_multiplier=2.0, expected_fraction=0.30)
    assert res_paced.triggered is True
    assert res_paced.direction == "BUY"
    assert res_paced.volume_ratio == 2.0
    assert res_paced.raw_volume_ratio == 0.6


def test_wick_filter_clv_rejection_and_acceptance():
    scanner = object.__new__(Scanner)
    scanner.signal_cfg = {
        "rsi": {"enabled": False},
        "macd": {"enabled": False},
        "ema_crossover": {"enabled": False},
        "volume_breakout": {"enabled": False},
    }
    scanner.universe_cfg = {"min_price": 10}
    scanner.confluence_cfg = {"mode": "crossover", "min_groups_to_alert": 1, "direction_consensus": True}
    scanner.guardrails_cfg = {
        "wick_filter": {
            "enabled": True,
            "min_clv_buy": 0.70,
            "max_clv_sell": 0.30,
            "apply_to_provisional": True,
            "apply_to_daily": False,
        }
    }

    # Reference data
    rows = [{"open": 100, "high": 105, "low": 95, "close": 100, "volume": 1000} for _ in range(35)]
    stock = SimpleNamespace(symbol="TCS", company_name="Tata Consultancy Services Ltd")

    # Mock confluence evaluation to always return BUY
    scanner.confluence_cfg["min_signals_to_alert"] = 1

    # Case A: Intraday Wick Trap (Day High 110, Day Low 90, Close 98).
    # Range = 20. CLV = (98 - 90) / 20 = 8 / 20 = 0.40 (< 0.70).
    # Large upper wick of 12 points! Must be rejected.
    wick_trap_row = {"open": 100, "high": 110, "low": 90, "close": 98, "volume": 1500}
    df_wick_trap = pd.DataFrame(rows + [wick_trap_row])

    # Fake a signal check so confluence triggers BUY
    scanner.signal_cfg["rsi"] = {"enabled": True, "oversold_threshold": 30, "overbought_threshold": 70}
    import src.scanner as scanner_module

    # Temporarily monkeypatch evaluate_confluence to return BUY
    old_eval = scanner_module.evaluate_confluence
    scanner_module.evaluate_confluence = lambda *args, **kwargs: {
        "direction": "BUY", "score": 2, "total_groups": 2,
        "group_votes": {"momentum": "BUY", "trend": "BUY"},
        "conflicting_groups": [], "checks": [{"name": "Mock", "met": True, "direction": "BUY"}],
    }

    try:
        # Evaluated as provisional -> rejected by wick filter
        res_rejected, _ = scanner._scan_stock(stock, df_wick_trap, provisional=True)
        assert res_rejected is None

        # Case B: Strong Close in top 20% of range (Day High 110, Day Low 90, Close 107).
        # Range = 20. CLV = (107 - 90) / 20 = 17 / 20 = 0.85 (>= 0.70).
        strong_row = {"open": 100, "high": 110, "low": 90, "close": 107, "volume": 1500}
        df_strong = pd.DataFrame(rows + [strong_row])

        res_passed, _ = scanner._scan_stock(stock, df_strong, provisional=True)
        assert res_passed is not None
        assert res_passed["direction"] == "BUY"
        assert res_passed["clv"] == 0.85
        assert res_passed["alert_tier"] == "intraday_radar"

        # Case C: Power Hour Scan at 15:15 IST with strong close
        res_power_hour, _ = scanner._scan_stock(stock, df_strong, provisional=True, is_power_hour=True)
        assert res_power_hour is not None
        assert res_power_hour["alert_tier"] == "power_hour_confirmed"
        assert res_power_hour["signal_type"] == "power_hour_confluence_buy"

    finally:
        scanner_module.evaluate_confluence = old_eval


def test_telegram_message_renders_candle_bar_strength_and_tier_badges():
    cfg = {
        "notifications": {
            "telegram": {
                "bot_token": "dummy_token",
                "chat_id": "dummy_chat_id",
            }
        }
    }
    notifier = TelegramNotifier(cfg)

    # 1. Tier 1 Intraday Radar Payload
    payload_radar = AlertPayload(
        symbol="INFY",
        company_name="Infosys Ltd",
        signal_type="provisional_intraday_confluence_buy",
        direction="BUY",
        price=1850.0,
        indicator_value=2.0,
        indicator_detail={"confluence_score": 2, "checks": [], "clv": 0.82},
        confluence_score=2,
        total_signals=3,
        provisional=True,
        alert_tier="intraday_radar",
        clv=0.82,
        quote_timestamp="2026-11-13T10:30:00+05:30",
    )
    msg_radar = notifier._format_message(payload_radar)
    assert "[INTRADAY RADAR]" in msg_radar
    assert "Watchlist Setup (Do Not Chase)" in msg_radar
    assert "Candle Bar Strength: <b>82%</b> (Top of day range; CLV 0.82)" in msg_radar

    # 2. Tier 2 Power Hour Confirmed Payload
    payload_power_hour = AlertPayload(
        symbol="INFY",
        company_name="Infosys Ltd",
        signal_type="power_hour_confluence_buy",
        direction="BUY",
        price=1862.0,
        indicator_value=2.0,
        indicator_detail={"confluence_score": 2, "checks": [], "clv": 0.91},
        confluence_score=2,
        total_signals=3,
        provisional=True,
        alert_tier="power_hour_confirmed",
        clv=0.91,
        quote_timestamp="2026-11-13T15:15:00+05:30",
    )
    msg_ph = notifier._format_message(payload_power_hour)
    assert "[3:15 PM CONFIRMED ENTRY]" in msg_ph
    assert "3:15 PM Confirmation Window" in msg_ph
    assert "Candle Bar Strength: <b>91%</b> (Top of day range; CLV 0.91)" in msg_ph


def test_main_power_hour_scheduled_job():
    import src.main as main_module
    assert hasattr(main_module, "run_power_hour_scan_job")
    assert callable(main_module.run_power_hour_scan_job)
