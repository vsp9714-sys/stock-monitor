"""
Tests for DB operations, deduplication, and config loading.
"""
from datetime import date, datetime, timezone
import os
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.config_loader import load_config, get_config
from src.notifiers.base import AlertPayload
from src.scanner import Scanner
from src.db import (
    Base, Stock, Signal, SignalDelivery, get_delivery_score,
    mark_delivery_sent, refresh_signal_notified_state, upsert_signal,
    seed_legacy_deliveries, create_sqlite_engine, OHLCVCache,
    DailyCandleRefresh, Fundamental, prune_market_data_to_symbols,
)


@pytest.fixture
def test_db_session(tmp_path):
    db_file = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def test_stock_master_crud(test_db_session):
    stock = Stock(
        symbol="INFY",
        isin="INE009A01021",
        company_name="Infosys Ltd",
        exchange="NSE",
        instrument_key="NSE_EQ|INE009A01021",
        series="EQ",
        is_active=True,
        last_updated=datetime.now(timezone.utc)
    )
    test_db_session.add(stock)
    test_db_session.commit()

    retrieved = test_db_session.query(Stock).filter_by(symbol="INFY").first()
    assert retrieved is not None
    assert retrieved.company_name == "Infosys Ltd"
    assert retrieved.exchange == "NSE"


def test_signal_deduplication(test_db_session):
    today = date(2026, 10, 20)
    # First insert
    sig1 = upsert_signal(
        session=test_db_session,
        symbol="RELIANCE",
        signal_date=today,
        signal_type="rsi_oversold",
        direction="BUY",
        price=1350.50,
        indicator_value=28.4,
        indicator_detail="RSI(14)=28.4 (< 30.0)",
    )
    assert sig1.id is not None

    # Second insert with identical symbol + date + type should return existing, not duplicate
    sig2 = upsert_signal(
        session=test_db_session,
        symbol="RELIANCE",
        signal_date=today,
        signal_type="rsi_oversold",
        direction="BUY",
        price=1352.00,
        indicator_value=29.1,
        indicator_detail="RSI(14)=29.1 (< 30.0)",
    )
    assert sig2.id == sig1.id

    # Total records in table should be 1
    total = test_db_session.query(Signal).count()
    assert total == 1


def test_confluence_signal_updates_to_highest_score_without_regression(test_db_session):
    today = date(2026, 10, 20)
    sig = upsert_signal(test_db_session, "RELIANCE", today, "confluence_buy", "BUY",
                        100.0, 2.0, '{"confluence_score": 2}')
    sig.notified = True
    test_db_session.commit()

    updated = upsert_signal(test_db_session, "RELIANCE", today, "confluence_buy", "BUY",
                            103.0, 3.0, '{"confluence_score": 3}')
    assert updated.id == sig.id
    assert updated.indicator_value == 3.0
    assert updated.price == 103.0
    assert updated.indicator_detail == '{"confluence_score": 3}'
    assert updated.notified is False

    stale = upsert_signal(test_db_session, "RELIANCE", today, "confluence_buy", "BUY",
                           99.0, 2.0, '{"confluence_score": 2}')
    assert stale.indicator_value == 3.0
    assert stale.price == 103.0


def test_confluence_score_upgrade_migrates_legacy_raw_and_delivery_scores(test_db_session):
    today = date(2026, 10, 20)
    legacy_detail = '{"confluence_score":3,"total_signals":5,"checks":[' \
        '{"signal_type":"ema_9x21","met":true,"direction":"BUY"},' \
        '{"signal_type":"ema_21x50","met":true,"direction":"BUY"},' \
        '{"signal_type":"ema_50x200","met":true,"direction":"BUY"},' \
        '{"signal_type":"volume_breakout","met":false,"direction":null}]}'
    signal = upsert_signal(test_db_session, "RELIANCE", today, "confluence_buy", "BUY",
                           100.0, 3.0, legacy_detail)
    mark_delivery_sent(test_db_session, signal.id, "telegram:individual", 3)

    upgraded = upsert_signal(
        test_db_session, "RELIANCE", today, "confluence_buy", "BUY", 101.0, 2.0,
        '{"score_version":2,"confluence_score":2,"total_signals":3,"checks":[]}',
    )
    detail = __import__("json").loads(upgraded.indicator_detail)
    assert upgraded.id == signal.id
    assert upgraded.indicator_value == 2.0
    assert detail["score_version"] == 2
    assert get_delivery_score(test_db_session, signal.id, "telegram:individual") == 1


def test_delivery_escalation_is_per_destination_and_score(test_db_session):
    sig = upsert_signal(test_db_session, "INFY", date(2026, 10, 20), "confluence_buy", "BUY",
                        100.0, 2.0, "{}")

    assert get_delivery_score(test_db_session, sig.id, "telegram:individual") is None
    mark_delivery_sent(test_db_session, sig.id, "telegram:individual", 2)
    mark_delivery_sent(test_db_session, sig.id, "telegram:group", 2)
    refresh_signal_notified_state(
        test_db_session, sig.id, ["telegram:individual", "telegram:group"]
    )
    assert sig.notified is True

    upsert_signal(test_db_session, "INFY", date(2026, 10, 20), "confluence_buy", "BUY",
                  101.0, 3.0, '{"confluence_score": 3}')
    mark_delivery_sent(test_db_session, sig.id, "telegram:individual", 3)
    refresh_signal_notified_state(
        test_db_session, sig.id, ["telegram:individual", "telegram:group"]
    )
    assert get_delivery_score(test_db_session, sig.id, "telegram:individual") == 3
    assert get_delivery_score(test_db_session, sig.id, "telegram:group") == 2
    assert sig.notified is False
    assert test_db_session.query(SignalDelivery).count() == 2


def test_legacy_migration_does_not_suppress_first_group_delivery(test_db_session):
    sig = upsert_signal(test_db_session, "INFY", date(2026, 10, 20), "confluence_buy", "BUY",
                        100.0, 2.0, "{}")
    sig.notified = True
    test_db_session.commit()

    seed_legacy_deliveries(
        test_db_session, ["telegram:individual", "telegram:group"]
    )
    assert get_delivery_score(test_db_session, sig.id, "telegram:individual") == 2
    assert get_delivery_score(test_db_session, sig.id, "telegram:group") is None


def test_scanner_sends_only_new_score_and_retries_only_failed_target(test_db_session):
    class StubNotifier:
        def __init__(self):
            self.calls = []
            self.fail_group_once = True

        def delivery_targets(self):
            return ["telegram:individual", "telegram:group"]

        def notify_target(self, payload, target, send_chart=True):
            self.calls.append((target, payload.confluence_score))
            if target == "telegram:group" and self.fail_group_once:
                self.fail_group_once = False
                return False
            return True

    scanner = object.__new__(Scanner)
    notifier = StubNotifier()
    scanner.notifiers = [notifier]
    signal = upsert_signal(test_db_session, "TCS", date(2026, 10, 20),
                           "confluence_buy", "BUY", 100.0, 2.0, "{}")

    def payload(score):
        return AlertPayload(
            symbol="TCS", company_name="TCS Ltd", signal_type="confluence_buy",
            direction="BUY", price=100.0, indicator_value=float(score),
            indicator_detail={}, confluence_score=score, total_signals=6,
        )

    assert scanner._dispatch_alert(payload(2), test_db_session, signal.id) is True
    assert notifier.calls == [("telegram:individual", 2), ("telegram:group", 2)]

    # The retry is target-scoped: don't resend to the recipient that succeeded.
    assert scanner._dispatch_alert(payload(2), test_db_session, signal.id) is True
    assert notifier.calls[-1] == ("telegram:group", 2)
    assert len(notifier.calls) == 3

    upsert_signal(test_db_session, "TCS", date(2026, 10, 20), "confluence_buy",
                  "BUY", 101.0, 3.0, '{"confluence_score": 3}')
    assert scanner._dispatch_alert(payload(3), test_db_session, signal.id) is True
    assert notifier.calls[-2:] == [("telegram:individual", 3), ("telegram:group", 3)]
    assert signal.indicator_value == 3.0
    assert signal.indicator_detail == '{"confluence_score": 3}'


def test_instrument_refresh_deactivates_non_equity_and_stale_rows(test_db_session, monkeypatch):
    test_db_session.add_all([
        Stock(symbol="805NMC35", isin="INE579F24073", exchange="NSE",
              instrument_key="NSE_EQ|INE579F24073", is_active=True),
        Stock(symbol="OLDCO", isin="INE000X01000", exchange="NSE",
              instrument_key="NSE_EQ|INE000X01000", is_active=True),
    ])
    test_db_session.commit()

    class Fetcher:
        def get_all_eq_symbols(self, exchange):
            assert exchange == "NSE"
            return [{
                "symbol": "ABC", "isin": "INE000A01000", "company_name": "ABC Limited",
                "exchange": "NSE", "instrument_key": "NSE_EQ|INE000A01000", "series": "EQ",
            }]

    monkeypatch.setattr("src.scanner.get_session", lambda: test_db_session)
    scanner = object.__new__(Scanner)
    scanner.fetcher = Fetcher()
    assert scanner.refresh_instrument_master() == 1
    assert test_db_session.query(Stock).filter_by(symbol="ABC").one().is_active is True
    assert test_db_session.query(Stock).filter_by(symbol="805NMC35").one().is_active is False
    assert test_db_session.query(Stock).filter_by(symbol="OLDCO").one().is_active is False


def test_instrument_refresh_replaces_stale_upstox_key(test_db_session, monkeypatch):
    test_db_session.add(Stock(
        symbol="KMEW", isin="INE0CJD01037", company_name="KMEW Limited",
        exchange="NSE", instrument_key="NSE_EQ|INE0CJD01029", series="EQ", is_active=True,
    ))
    test_db_session.commit()

    class Fetcher:
        def get_all_eq_symbols(self, exchange):
            assert exchange == "NSE"
            return [{
                "symbol": "KMEW", "isin": "INE0CJD01037", "company_name": "KMEW Limited",
                "exchange": "NSE", "instrument_key": "NSE_EQ|INE0CJD01037", "series": "EQ",
            }]

    monkeypatch.setattr("src.scanner.get_session", lambda: test_db_session)
    scanner = object.__new__(Scanner)
    scanner.fetcher = Fetcher()
    assert scanner.refresh_instrument_master() == 1
    assert test_db_session.query(Stock).filter_by(symbol="KMEW").one().instrument_key == "NSE_EQ|INE0CJD01037"


def test_sqlite_engine_uses_wal_and_busy_timeout(tmp_path):
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'concurrency.db'}")
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar().lower() == "wal"
            assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar() == 30000
    finally:
        engine.dispose()


def test_prune_market_data_preserves_signal_and_selected_symbol(test_db_session):
    test_db_session.add_all([
        OHLCVCache(symbol="KEEP", interval="1d", ts=datetime(2026, 11, 5), close=10),
        OHLCVCache(symbol="DROP", interval="1d", ts=datetime(2026, 11, 5), close=20),
        DailyCandleRefresh(symbol="KEEP", session_date=date(2026, 11, 5), status="confirmed"),
        DailyCandleRefresh(symbol="DROP", session_date=date(2026, 11, 5), status="no_bar"),
        Fundamental(symbol="KEEP", fetched_at=datetime.now(timezone.utc)),
        Fundamental(symbol="DROP", fetched_at=datetime.now(timezone.utc)),
        Signal(symbol="DROP", signal_date=date(2026, 11, 5), signal_type="test", direction="BUY"),
    ])
    test_db_session.commit()
    with pytest.raises(ValueError, match="empty universe"):
        prune_market_data_to_symbols(test_db_session, set())
    deleted = prune_market_data_to_symbols(test_db_session, {"KEEP"})
    assert deleted == {"ohlcv_cache": 1, "daily_candle_refresh": 1, "fundamentals": 1}
    assert [row.symbol for row in test_db_session.query(OHLCVCache).all()] == ["KEEP"]
    assert [row.symbol for row in test_db_session.query(Signal).all()] == ["DROP"]


def test_config_loader():
    cfg = load_config()
    assert "upstox" in cfg
    assert "signals" in cfg
    assert "database" in cfg
    assert "notifications" in cfg
    assert "market" in cfg
    assert "universe" in cfg

    # Verify trading hours in market config
    assert cfg["market"]["open_time"] == "09:15"
    assert cfg["market"]["close_time"] == "15:30"
    assert cfg["market"]["timezone"] == "Asia/Kolkata"
    assert cfg["universe"]["mode"] == "nifty500"


def test_signal_detail_serializes_numpy_and_pandas_types(test_db_session):
    """Issue #25 regression test: NumPy booleans and scalars must persist cleanly."""
    import json
    import numpy as np
    import pandas as pd
    from src.scanner import _json_default

    detail = {
        "signal_horizon": "provisional_intraday",
        "confluence_score": np.int64(3),
        "total_signals": 5,
        "checks": [
            {
                "name": "EMA 9×21",
                "state_met": np.bool_(True),
                "event_met": np.bool_(False),
                "value": np.float64(1420.5),
            },
            {
                "name": "RSI (14)",
                "state_met": True,
                "event_met": False,
                "value": 28.5,
            },
        ],
        "session_date": pd.Timestamp("2026-11-09").date(),
        "tags": {"swing", "momentum"},
    }

    serialized = json.dumps(detail, default=_json_default)
    assert serialized is not None

    sig = upsert_signal(
        session=test_db_session,
        symbol="ASTRAL",
        signal_date=date(2026, 11, 9),
        signal_type="confluence_buy",
        direction="BUY",
        price=1350.0,
        indicator_value=3.0,
        indicator_detail=serialized,
    )
    assert sig.id is not None
    loaded = json.loads(sig.indicator_detail)
    assert loaded["checks"][0]["state_met"] is True
    assert loaded["checks"][0]["value"] == 1420.5
    assert loaded["session_date"] == "2026-11-09"
