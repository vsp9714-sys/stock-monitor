import pytest
from types import SimpleNamespace
import pandas as pd

from src.config_loader import validate_signal_config
from src.scanner import Scanner
from src.signals.volume_breakout import VolumeBreakoutResult
from src.signals.confluence import evaluate_confluence


def _check(signal_type, *, event=False, event_direction=None, state=False, state_direction=None):
    return {
        "signal_type": signal_type,
        "met": event,
        "direction": event_direction,
        "event_met": event,
        "event_direction": event_direction,
        "state_met": state,
        "state_direction": state_direction,
    }


def test_three_ema_crosses_are_one_independent_trend_vote():
    checks = [
        _check("ema_9x21", event=True, event_direction="BUY"),
        _check("ema_21x50", event=True, event_direction="BUY"),
        _check("ema_50x200", event=True, event_direction="BUY"),
        _check("macd", event=True, event_direction="BUY"),
    ]
    result = evaluate_confluence(checks, minimum_groups=2)
    assert result["direction"] == "BUY"
    assert result["score"] == 2
    assert result["total_groups"] == 2
    assert result["group_votes"] == {"trend": "BUY", "momentum": "BUY"}
    assert sum(check["group_vote"] for check in result["checks"]) == 2


def test_ema_crosses_alone_cannot_satisfy_two_group_threshold():
    checks = [
        _check("ema_9x21", event=True, event_direction="BUY"),
        _check("ema_21x50", event=True, event_direction="BUY"),
        _check("ema_50x200", event=True, event_direction="BUY"),
    ]
    result = evaluate_confluence(checks, minimum_groups=2)
    assert result["direction"] is None
    assert result["score"] == 1


def test_trend_and_participation_groups_meet_threshold_independently():
    checks = [
        _check("ema_9x21", event=True, event_direction="BUY"),
        _check("volume_breakout", event=True, event_direction="BUY"),
    ]
    result = evaluate_confluence(checks, minimum_groups=2)
    assert result["direction"] == "BUY"
    assert result["group_votes"] == {"trend": "BUY", "participation": "BUY"}


def test_trend_mode_allows_aligned_continuation_but_crossover_requires_event():
    checks = [
        _check("ema_9x21", state=True, state_direction="BUY"),
        _check("macd", state=True, state_direction="BUY"),
    ]
    cross = evaluate_confluence(checks, mode="crossover", minimum_groups=2)
    trend = evaluate_confluence(checks, mode="trend", minimum_groups=2)
    assert cross["direction"] is None
    assert trend["direction"] == "BUY"
    assert trend["score"] == 2


def test_opposing_groups_follow_direction_consensus_setting():
    checks = [
        _check("ema_9x21", event=True, event_direction="BUY"),
        _check("macd", event=True, event_direction="BUY"),
        _check("volume_breakout", event=True, event_direction="SELL"),
    ]
    strict = evaluate_confluence(checks, minimum_groups=1, direction_consensus=True)
    relaxed = evaluate_confluence(checks, minimum_groups=1, direction_consensus=False)
    assert strict["direction"] is None
    assert relaxed["direction"] == "BUY"
    assert relaxed["score"] == 2


def test_conflicting_checks_inside_one_group_do_not_vote():
    checks = [
        _check("ema_9x21", event=True, event_direction="BUY"),
        _check("ema_21x50", event=True, event_direction="SELL"),
        _check("macd", event=True, event_direction="BUY"),
    ]
    result = evaluate_confluence(checks, minimum_groups=1)
    assert result["direction"] == "BUY"
    assert result["score"] == 1
    assert result["conflicting_groups"] == ["trend"]
    assert not any(check["group_vote"] for check in result["checks"] if check["group"] == "trend")


def test_config_rejects_unsupported_modes_and_group_thresholds():
    with pytest.raises(ValueError, match="confluence.mode"):
        validate_signal_config({"confluence": {"mode": "momentum"}})
    with pytest.raises(ValueError, match="min_groups_to_alert"):
        validate_signal_config({"confluence": {"min_groups_to_alert": 4}})
    with pytest.raises(ValueError, match="signals.rsi.mode"):
        validate_signal_config({"signals": {"rsi": {"mode": "guess"}}})


def test_config_accepts_explicit_legacy_minimum_and_rsi_mode():
    validate_signal_config({
        "confluence": {"mode": "crossover", "min_signals_to_alert": 2},
        "signals": {"rsi": {"mode": "mean_reversion"}},
    })


def test_provisional_volume_keeps_user_accepted_full_day_baseline(monkeypatch):
    scanner = object.__new__(Scanner)
    scanner.signal_cfg = {
        "rsi": {"enabled": False},
        "macd": {"enabled": False},
        "ema_crossover": {"enabled": False},
        "volume_breakout": {"enabled": True},
    }
    scanner.universe_cfg = {"min_price": 10}
    scanner.confluence_cfg = {
        "mode": "crossover", "min_groups_to_alert": 1,
        "direction_consensus": True,
    }
    monkeypatch.setattr(
        "src.scanner.detect_volume_breakout",
        lambda *args, **kwargs: VolumeBreakoutResult(
            symbol="ABC", signal_type="volume_breakout_bullish", direction="BUY",
            current_volume=1000, avg_volume=500, volume_ratio=2, current_price=100,
            breakout_level=99, triggered=True,
        ),
    )
    candles = pd.DataFrame({"close": [100.0] * 40})
    result, _ = scanner._scan_stock(SimpleNamespace(symbol="ABC", company_name="ABC Ltd"),
                                    candles, provisional=True)
    assert result is not None
    volume_check = next(check for check in result["checks"] if check["signal_type"] == "volume_breakout")
    assert volume_check["met"] is True
    assert "partial-session volume vs full-day history" in volume_check["description"]
