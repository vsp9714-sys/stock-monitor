from datetime import date, datetime
import pytest

import pandas as pd

from src.backtest.engine import simulate_period
from src.risk import atr_value, risk_reference


def _bars(count=35):
    dates = pd.date_range("2026-01-01", periods=count, freq="D")
    rows = [{"ts": day, "open": 100.0, "high": 101.0, "low": 99.0,
             "close": 100.0, "volume": 1000.0} for day in dates]
    return pd.DataFrame(rows)


def test_atr_risk_references_for_long_and_short_and_missing_account():
    frame = _bars()
    assert atr_value(frame, period=2) == 2.0
    long = risk_reference(frame, entry_price=100, direction="BUY",
                          config={"atr_period": 2, "stop_atr_multiple": 1.5, "target_r_multiple": 2})
    short = risk_reference(frame, entry_price=100, direction="SELL",
                           config={"atr_period": 2, "stop_atr_multiple": 1.5, "target_r_multiple": 2})
    assert long["stop_reference"] == 97
    assert long["target_reference"] == 106
    assert short["stop_reference"] == 103
    assert short["target_reference"] == 94
    assert long["position_size_status"] == "unconfigured"


def test_risk_sizing_requires_explicit_capital_and_positive_distance():
    frame = _bars()
    sized = risk_reference(frame, entry_price=100, direction="BUY",
                           config={"atr_period": 2, "account_capital_inr": 100000, "risk_percent": 1})
    assert sized["quantity"] == 333
    invalid = risk_reference(frame, entry_price=1, direction="BUY",
                             config={"atr_period": 2, "stop_atr_multiple": 2})
    assert invalid["status"] == "unavailable"


def test_atr_warmup_is_unavailable_and_disabled_risk_is_explicit():
    frame = _bars(5)
    assert atr_value(frame, period=5) is None
    unavailable = risk_reference(frame, entry_price=100, direction="BUY", config={"atr_period": 5})
    assert unavailable["reason"] == "atr_history_unavailable"
    assert risk_reference(frame, entry_price=100, direction="BUY", config={"enabled": False})["status"] == "disabled"


def test_backtest_uses_signal_close_then_next_open_and_stop_first_without_lookahead():
    frame = _bars()
    # The first entry bar hits both the 1.5-ATR stop and 2R target; conservative
    # same-bar handling must record the stop. Make the signal bar itself harmless.
    frame.loc[30, ["high", "low", "close"]] = [107.0, 96.0, 100.0]
    seen_dates = []

    def evaluate(symbol, prefix):
        seen_dates.append(prefix["ts"].max())
        if len(prefix) == 30:
            return {"direction": "BUY"}
        return None

    result = simulate_period(
        {"AAA": frame}, evaluate, start_date=date(2026, 1, 1), end_date=date(2026, 2, 4),
        config={"backtest": {"atr_period": 2, "stop_atr_multiple": 1.5,
                             "target_r_multiple": 2, "fee_bps_per_side": 0,
                             "slippage_bps_per_side": 0}},
    )
    assert result["summary"]["trade_count"] == 1
    trade = result["trades"][0]
    assert trade["signal_date"] == "2026-01-30"
    assert trade["entry_date"] == "2026-01-31"
    assert trade["exit_reason"] == "stop"
    assert trade["exit_price"] == 97
    assert max(seen_dates) <= pd.Timestamp("2026-02-04")


def test_backtest_costs_reduce_net_return_and_metrics_are_reproducible():
    frame = _bars()
    frame.loc[30:, ["open", "high", "low", "close"]] = [100, 110, 99, 108]

    def evaluate(_symbol, prefix):
        return {"direction": "BUY"} if len(prefix) == 30 else None

    result = simulate_period(
        {"AAA": frame}, evaluate, start_date=date(2026, 1, 1), end_date=date(2026, 2, 4),
        config={"backtest": {"atr_period": 2, "stop_atr_multiple": 1.5,
                             "target_r_multiple": 2, "fee_bps_per_side": 5,
                             "slippage_bps_per_side": 5}},
    )
    assert result["trades"][0]["exit_reason"] == "target"
    assert result["trades"][0]["net_return_pct"] < result["trades"][0]["gross_return_pct"]
    assert result["summary"]["wins"] == 1


def test_short_gap_through_stop_fills_at_open_and_costs_use_actual_fills():
    frame = _bars()
    # A short entered at 100 with ATR 2 has a stop at 103. The next bar gaps
    # above it, so conservative execution exits at the open, not at the stop.
    frame.loc[31, ["open", "high", "low", "close"]] = [106, 108, 104, 107]

    def evaluate(_symbol, prefix):
        return {"direction": "SELL"} if len(prefix) == 30 else None

    result = simulate_period(
        {"AAA": frame}, evaluate, start_date=date(2026, 1, 1), end_date=date(2026, 2, 4),
        config={"backtest": {"atr_period": 2, "stop_atr_multiple": 1.5,
                             "target_r_multiple": 2, "fee_bps_per_side": 5,
                             "slippage_bps_per_side": 10}},
    )
    trade = result["trades"][0]
    assert trade["exit_reason"] == "stop"
    assert trade["exit_price"] > 106  # gap open plus adverse slippage
    assert trade["net_return_pct"] == trade["gross_return_pct"] - trade["cost_pct"]


def test_risk_reference_tranche_capping_and_risk_distance():
    frame = _bars()
    # entry 100, ATR 2, stop 1.5 ATR -> distance = 3.0, risk_distance_pct = 3.0%
    # capital 500,000, risk 1% -> risk budget = 5,000 INR
    # qty_by_risk = floor(5000 / 3) = 1666 shares
    # notional = 1666 * 100 = 166,600 INR (> max_tranche 100,000 INR)
    # With max_tranche_inr = 100000:
    # qty_by_tranche = floor(100000 / 100) = 1000 shares
    # quantity = min(1666, 1000) = 1000 shares (tranche_capped)
    cfg = {
        "enabled": True,
        "atr_period": 2,
        "stop_atr_multiple": 1.5,
        "target_r_multiple": 2.0,
        "account_capital_inr": 500000,
        "risk_percent": 1.0,
        "max_tranche_inr": 100000,
        "max_risk_distance_pct": 6.0,
    }
    sized = risk_reference(frame, entry_price=100.0, direction="BUY", config=cfg)
    assert sized["quantity"] == 1000
    assert sized["sizing_constraint"] == "tranche_capped"
    assert sized["notional_inr"] == 100000.0
    assert sized["actual_risk_inr"] == 3000.0
    assert sized["risk_distance_pct"] == 3.0
    assert sized["fits_risk_distance_cap"] is True

    # When risk distance is wide, e.g. entry 100, ATR 5, stop 1.5 ATR -> distance = 7.5 (risk_dist_pct = 7.5%)
    # Under standardized cap (max_risk_distance_pct=6.0), this setup is rejected as unavailable!
    frame_wide = _bars()
    frame_wide.loc[:, "high"] = 102.5
    frame_wide.loc[:, "low"] = 97.5
    sized_wide = risk_reference(frame_wide, entry_price=100.0, direction="BUY", config=cfg)
    assert sized_wide["status"] == "unavailable"
    assert sized_wide["reason"] == "risk_distance_exceeds_max"
    assert sized_wide["risk_distance_pct"] == 7.5
    assert sized_wide["max_risk_distance_pct"] == 6.0


def test_simulate_period_max_hold_days_and_allowed_directions():
    frame = _bars(45)
    # Set prices so neither stop nor target is hit
    frame.loc[30:, "high"] = 101.0
    frame.loc[30:, "low"] = 99.0
    frame.loc[30:, "close"] = 100.5

    def evaluate(_symbol, prefix):
        if len(prefix) == 30:
            return {"direction": "BUY"}
        return None

    # With max_hold_days = 5
    res = simulate_period(
        {"AAA": frame}, evaluate, start_date=date(2026, 1, 1), end_date=date(2026, 2, 14),
        config={"backtest": {
            "atr_period": 2, "stop_atr_multiple": 1.5, "target_r_multiple": 2.0,
            "max_hold_days": 5, "allowed_directions": ["BUY"],
        }},
    )
    assert len(res["trades"]) == 1
    trade = res["trades"][0]
    assert trade["exit_reason"] == "time_expiry"
    assert trade["holding_sessions"] == 5
    assert "profit_factor" in res["summary"]
    assert "expectancy_r" in res["summary"]

    # When allowed_directions contains only SELL, the BUY signal is skipped
    res_skip = simulate_period(
        {"AAA": frame}, evaluate, start_date=date(2026, 1, 1), end_date=date(2026, 2, 14),
        config={"backtest": {
            "atr_period": 2, "stop_atr_multiple": 1.5, "target_r_multiple": 2.0,
            "allowed_directions": ["SELL"],
        }},
    )
    assert len(res_skip["trades"]) == 0


def test_run_backtest_sets_guardrails_cfg_and_changes_config_hash(monkeypatch, tmp_path):
    """Verify run_backtest() sets evaluator.guardrails_cfg and changes config_hash_sha256 when guardrails change."""
    from datetime import datetime, timedelta, date
    from src.backtest.cli import run_backtest
    import src.backtest.cli as cli_mod
    from src.db import Base, create_sqlite_engine, Stock, OHLCVCache
    from sqlalchemy.orm import sessionmaker

    engine = create_sqlite_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    stock = Stock(symbol="SBIN", company_name="State Bank", is_active=True)
    session.add(stock)
    for i in range(40):
        c = OHLCVCache(
            symbol="SBIN", interval="1d",
            ts=datetime(2026, 1, 1) + timedelta(days=i),
            open=500.0, high=510.0, low=495.0, close=505.0, volume=10000,
        )
        session.add(c)
    session.commit()

    evaluator_instances = []
    orig_scan = cli_mod.Scanner._scan_stock

    def fake_scan(self, stock, frame, provisional=False):
        evaluator_instances.append(self)
        return orig_scan(self, stock, frame, provisional=provisional)

    monkeypatch.setattr(cli_mod.Scanner, "_scan_stock", fake_scan)
    monkeypatch.setattr(cli_mod, "get_session", lambda: Session())

    cfg1 = {
        "signals": {}, "confluence": {},
        "guardrails": {"adx_filter": {"enabled": True, "min_adx": 20.0}},
        "backtest": {"train_fraction": 0.7},
        "risk": {},
    }
    monkeypatch.setattr(cli_mod, "get_config", lambda: cfg1)

    rep1 = run_backtest(date(2026, 1, 30), date(2026, 2, 8), ["SBIN"], tmp_path / "bt1")
    assert evaluator_instances[-1].guardrails_cfg == cfg1["guardrails"]
    hash1 = rep1["config_hash_sha256"]

    # Now change guardrails.adx_filter.min_adx
    cfg2 = {
        "signals": {}, "confluence": {},
        "guardrails": {"adx_filter": {"enabled": True, "min_adx": 25.0}},
        "backtest": {"train_fraction": 0.7},
        "risk": {},
    }
    monkeypatch.setattr(cli_mod, "get_config", lambda: cfg2)

    rep2 = run_backtest(date(2026, 1, 30), date(2026, 2, 8), ["SBIN"], tmp_path / "bt2")
    assert evaluator_instances[-1].guardrails_cfg == cfg2["guardrails"]
    hash2 = rep2["config_hash_sha256"]

    assert hash1 != hash2


def test_risk_reference_max_risk_distance_cap_variations():
    """Verify max_risk_distance_pct behavior across None, higher cap, and lower cap."""
    frame = _bars()
    # entry 100, ATR 2, stop 2.0 ATR -> distance = 4.0, risk_dist_pct = 4.0%
    base_cfg = {
        "enabled": True,
        "atr_period": 2,
        "stop_atr_multiple": 2.0,
        "target_r_multiple": 1.5,
    }

    # Case 1: max_risk_distance_pct is None (uncapped)
    res_none = risk_reference(frame, entry_price=100.0, direction="BUY", config=base_cfg)
    assert res_none["status"] == "available"
    assert res_none["risk_distance_pct"] == 4.0
    assert "max_risk_distance_pct" not in res_none

    # Case 2: max_risk_distance_pct is 8.0 (risk_dist_pct 4.0 <= 8.0)
    res_pass = risk_reference(
        frame, entry_price=100.0, direction="BUY",
        config={**base_cfg, "max_risk_distance_pct": 8.0},
    )
    assert res_pass["status"] == "available"
    assert res_pass["risk_distance_pct"] == 4.0
    assert res_pass["max_risk_distance_pct"] == 8.0
    assert res_pass["fits_risk_distance_cap"] is True

    # Case 3: max_risk_distance_pct is 3.0 (risk_dist_pct 4.0 > 3.0) -> rejected
    res_fail = risk_reference(
        frame, entry_price=100.0, direction="BUY",
        config={**base_cfg, "max_risk_distance_pct": 3.0},
    )
    assert res_fail["status"] == "unavailable"
    assert res_fail["reason"] == "risk_distance_exceeds_max"
    assert res_fail["risk_distance_pct"] == 4.0
    assert res_fail["max_risk_distance_pct"] == 3.0


def test_simulate_period_skips_wide_atr_on_risk_distance_cap():
    """Verify backtest engine skips signals exceeding max_risk_distance_pct and tracks count."""
    frame = _bars(45)

    def evaluate(_symbol, prefix):
        if len(prefix) == 30:
            return {"direction": "BUY"}
        return None

    # Entry is 100, ATR is 2, stop is 2.0 ATR -> distance 4.0 (4.0% risk distance).
    # With max_risk_distance_pct = 3.0%, the trade must be skipped.
    res = simulate_period(
        {"AAA": frame}, evaluate, start_date=date(2026, 1, 1), end_date=date(2026, 2, 14),
        config={"backtest": {
            "atr_period": 2, "stop_atr_multiple": 2.0, "target_r_multiple": 1.5,
            "max_risk_distance_pct": 3.0, "allowed_directions": ["BUY"],
        }},
    )
    assert len(res["trades"]) == 0
    assert res["summary"]["skipped"]["risk_distance_exceeds_max"] == 1


def test_scanner_suppresses_alerts_on_risk_distance_exceeds_max(monkeypatch):
    """Verify live Scanner suppresses alerts and creates no paper trade when risk distance exceeds cap."""
    from src.scanner import Scanner
    from src.db import Base, create_sqlite_engine, Stock, PaperTrade
    from sqlalchemy.orm import sessionmaker

    engine = create_sqlite_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    stock = Stock(symbol="WIDE_RISK", company_name="Wide Risk Co", is_active=True, instrument_key="NSE_EQ|WIDE_RISK")
    session.add(stock)
    session.commit()

    scanner = object.__new__(Scanner)
    scanner.cfg = {
        "upstox": {"quote_batch_size": 100, "max_quote_age_seconds": 999999},
        "scan": {"min_coverage": 0.0},
        "universe": {"tradability": {"enabled": False}},
        "signals": {}, "guardrails": {}, "confluence": {},
        "paper_trading": {"enabled": True, "strategy_version": "v2_p3_5_bear_rs50"},
        "risk": {
            "enabled": True,
            "atr_period": 14,
            "stop_atr_multiple": 2.0,
            "target_r_multiple": 1.5,
            "max_risk_distance_pct": 5.0,  # Cap at 5.0%
        },
    }
    scanner.paper_trading_cfg = scanner.cfg["paper_trading"]
    scanner.universe_cfg = scanner.cfg["universe"]
    scanner.scan_cfg = scanner.cfg["scan"]
    scanner.signal_cfg = scanner.cfg["signals"]
    scanner.guardrails_cfg = scanner.cfg["guardrails"]
    scanner.confluence_cfg = scanner.cfg["confluence"]
    scanner.notifiers = []
    scanner.notifiers_cfg = {}

    class FakeFund:
        def get_fundamentals(self, symbol, isin):
            return None

    scanner.fundamentals = FakeFund()
    scanner.cfg["market_context"] = {"enabled": False}
    scanner.cfg["derivatives"] = {"enabled": False}

    # Candidate with high ATR: entry 100, ATR 5.0 -> 2.0 * ATR = 10.0 (10.0% > 5.0% cap)
    frame = _bars(40)
    frame.loc[:, "high"] = 105.0
    frame.loc[:, "low"] = 95.0
    frame.loc[:, "close"] = 100.0

    class FakeFetcher:
        def get_full_market_quotes(self, keys, **kwargs):
            from datetime import timezone
            now_ts = int(datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc).timestamp() * 1000)
            return {
                "NSE_EQ|WIDE_RISK": {
                    "open": 100.0,
                    "high": 105.0,
                    "low": 95.0,
                    "close": 100.0,
                    "last_price": 100.0,
                    "prev_close_price": 100.0,
                    "volume": 10000.0,
                    "last_trade_time": now_ts,
                    "ts": now_ts,
                }
            }

    scanner.fetcher = FakeFetcher()
    dispatched_alerts = []
    scanner._dispatch_alert = lambda payload, db, sig_id: dispatched_alerts.append(payload) or True
    scanner.get_active_stocks = lambda limit=None: [stock]
    scanner.required_daily_candles = lambda: 30
    scanner._load_cached_daily_frames = lambda stocks, db: {"WIDE_RISK": frame}

    # Signal evaluation returns a BUY confluence candidate
    def fake_scan(stock_arg, df, provisional=False, **kwargs):
        return {
            "symbol": stock_arg.symbol,
            "signal_type": "CONFLUENCE_BUY",
            "direction": "BUY",
            "confluence_score": 3,
            "total_signals": 4,
            "checks": {},
        }, df

    scanner._scan_stock = fake_scan
    monkeypatch.setattr("src.scanner.get_session", lambda: session)
    monkeypatch.setattr("src.scanner.is_market_open", lambda *args, **kwargs: True)

    from datetime import timezone
    summary = scanner.run_scan(now=datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc))

    # Assert candidate was suppressed
    assert len(dispatched_alerts) == 0
    assert summary.universe_metadata.get("risk_distance_filtered_count") >= 1
    # Assert no paper trade was recorded
    paper_trades = session.query(PaperTrade).filter_by(symbol="WIDE_RISK").all()
    assert len(paper_trades) == 0


def test_portfolio_5l_simulation_math():
    """Verify 5-slot simulator math: ending_capital == starting_capital + sum(pnl) and losses cannot produce positive ROI."""
    from scripts.run_h1_and_ytd_verification import simulate_5slot_portfolio

    # 1. Test losing trades set: 2 losing trades
    losing_trades = [
        {
            "symbol": "LOSS1",
            "entry_date": "2026-02-01",
            "exit_date": "2026-02-05",
            "entry_price": 100.0,
            "exit_price": 95.0,
            "gross_return_pct": -5.0,
            "atr_at_signal": 2.5,
            "rs_rating_at_signal": 70,
        },
        {
            "symbol": "LOSS2",
            "entry_date": "2026-02-02",
            "exit_date": "2026-02-06",
            "entry_price": 200.0,
            "exit_price": 190.0,
            "gross_return_pct": -5.0,
            "atr_at_signal": 5.0,
            "rs_rating_at_signal": 65,
        },
    ]
    res_loss = simulate_5slot_portfolio(losing_trades, starting_capital=500_000.0)
    assert res_loss["trades_taken"] == 2
    assert res_loss["net_pnl_inr"] < 0
    assert res_loss["roi_pct"] < 0
    assert res_loss["ending_capital_inr"] < 500_000.0
    assert res_loss["ending_capital_inr"] == pytest.approx(500_000.0 + res_loss["net_pnl_inr"], rel=1e-5)

    # 2. Test mixed trades set
    mixed_trades = [
        {
            "symbol": "WIN1",
            "entry_date": "2026-01-05",
            "exit_date": "2026-01-15",
            "entry_price": 100.0,
            "exit_price": 110.0,
            "gross_return_pct": 10.0,
            "atr_at_signal": 2.0,
            "rs_rating_at_signal": 80,
        },
        {
            "symbol": "LOSS3",
            "entry_date": "2026-01-10",
            "exit_date": "2026-01-20",
            "entry_price": 50.0,
            "exit_price": 48.0,
            "gross_return_pct": -4.0,
            "atr_at_signal": 1.0,
            "rs_rating_at_signal": 75,
        },
    ]
    res_mixed = simulate_5slot_portfolio(mixed_trades, starting_capital=500_000.0)
    assert res_mixed["ending_capital_inr"] == pytest.approx(500_000.0 + res_mixed["net_pnl_inr"], rel=1e-5)

    # 3. Test 5-slot constraint: 6 simultaneous trades on same day
    six_trades = [
        {
            "symbol": f"STOCK{i}",
            "entry_date": "2026-03-01",
            "exit_date": "2026-03-10",
            "entry_price": 100.0,
            "exit_price": 105.0,
            "gross_return_pct": 5.0,
            "atr_at_signal": 2.0,
            "rs_rating_at_signal": 50 + i,
        }
        for i in range(6)
    ]
    res_six = simulate_5slot_portfolio(six_trades, starting_capital=500_000.0)
    assert res_six["trades_taken"] == 5
    assert res_six["trades_skipped_slots"] == 1
    assert res_six["ending_capital_inr"] == pytest.approx(500_000.0 + res_six["net_pnl_inr"], rel=1e-5)
