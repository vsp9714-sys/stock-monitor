"""
Tests for Paper Trading & Validation Framework.
"""
from datetime import date, datetime, timedelta, timezone
from typing import Any
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.db import Base, PaperTrade, PaperTradeMilestone, PaperTradeWeeklyReport
from src.paper_trading import (
    record_paper_trade_entry,
    monitor_open_paper_trades,
    generate_weekly_report,
)
from src.notifiers.base import BaseNotifier, AlertPayload


class DummyNotifier(BaseNotifier):
    def __init__(self):
        self.sent_alerts = []
        self.broadcasts = []

    def send_alert(self, payload: AlertPayload) -> bool:
        self.sent_alerts.append(payload)
        return True

    def send_alert_with_chart(self, payload: AlertPayload) -> bool:
        self.sent_alerts.append(payload)
        return True

    def broadcast_message(self, message: str) -> bool:
        self.broadcasts.append(message)
        return True


@pytest.fixture
def db_session():
    """Create in-memory SQLite database and session for testing."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def sample_config():
    return {
        "paper_trading": {
            "enabled": True,
            "max_hold_days": 10,
            "milestones": [2.5, -2.5, 5.0, -5.0],
            "send_milestone_alerts": True,
            "send_exit_alerts": True,
            "cooldown_days": 3,
        }
    }


class TestPaperTradeEntryCreation:
    def test_record_paper_trade_entry_with_risk_ref(self, db_session, sample_config):
        risk_ref = {
            "status": "available",
            "stop_reference": 980.0,
            "target_reference": 1040.0,
            "atr": 13.33,
            "risk_distance": 20.0,
        }
        confluence_res = {
            "confluence_score": 2,
            "group_votes": {"trend": "BUY", "momentum": "BUY"},
        }
        trade = record_paper_trade_entry(
            session=db_session,
            signal_id=1,
            symbol="RELIANCE",
            direction="BUY",
            entry_price=1000.0,
            risk_ref=risk_ref,
            confluence_result=confluence_res,
            alert_tier="confirmed_daily",
            session_date=date(2026, 11, 20),
            config=sample_config,
        )
        assert trade is not None
        assert trade.symbol == "RELIANCE"
        assert trade.direction == "BUY"
        assert trade.entry_price == 1000.0
        assert trade.stop_loss == 980.0
        assert trade.target_price == 1040.0
        assert trade.atr_at_entry == 13.33
        assert trade.risk_distance == 20.0
        assert trade.status == "open"
        assert trade.confluence_score == 2
        assert trade.alert_tier == "confirmed_daily"
        assert trade.max_favorable_excursion == 1000.0
        assert trade.max_adverse_excursion == 1000.0

    def test_record_paper_trade_dedup_same_day(self, db_session, sample_config):
        risk_ref = {"status": "available", "stop_reference": 980.0, "target_reference": 1040.0}
        confluence_res = {"confluence_score": 2}
        t1 = record_paper_trade_entry(
            session=db_session,
            signal_id=1,
            symbol="INFY",
            direction="BUY",
            entry_price=1500.0,
            risk_ref=risk_ref,
            confluence_result=confluence_res,
            alert_tier="intraday_radar",
            session_date=date(2026, 11, 20),
            config=sample_config,
        )
        assert t1 is not None

        # Second attempt on same session date should not create a second row
        t2 = record_paper_trade_entry(
            session=db_session,
            signal_id=2,
            symbol="INFY",
            direction="BUY",
            entry_price=1505.0,
            risk_ref=risk_ref,
            confluence_result=confluence_res,
            alert_tier="confirmed_daily",
            session_date=date(2026, 11, 20),
            config=sample_config,
        )
        assert t2.id == t1.id
        assert db_session.query(PaperTrade).filter_by(symbol="INFY").count() == 1

    def test_record_paper_trade_open_guard(self, db_session, sample_config):
        risk_ref = {"status": "available", "stop_reference": 980.0, "target_reference": 1040.0}
        confluence_res = {"confluence_score": 2}
        t1 = record_paper_trade_entry(
            session=db_session,
            signal_id=1,
            symbol="TCS",
            direction="BUY",
            entry_price=3500.0,
            risk_ref=risk_ref,
            confluence_result=confluence_res,
            alert_tier="confirmed_daily",
            session_date=date(2026, 11, 15),
            config=sample_config,
        )
        assert t1 is not None

        # Next trading day: trade 1 is still open. Should not enter duplicate open trade.
        t2 = record_paper_trade_entry(
            session=db_session,
            signal_id=2,
            symbol="TCS",
            direction="BUY",
            entry_price=3520.0,
            risk_ref=risk_ref,
            confluence_result=confluence_res,
            alert_tier="confirmed_daily",
            session_date=date(2026, 11, 16),
            config=sample_config,
        )
        assert t2 is None
        assert db_session.query(PaperTrade).filter_by(symbol="TCS").count() == 1

    def test_record_paper_trade_cooldown(self, db_session, sample_config):
        risk_ref = {"status": "available", "stop_reference": 980.0, "target_reference": 1040.0}
        confluence_res = {"confluence_score": 2}
        t1 = record_paper_trade_entry(
            session=db_session,
            signal_id=1,
            symbol="WIPRO",
            direction="BUY",
            entry_price=500.0,
            risk_ref=risk_ref,
            confluence_result=confluence_res,
            alert_tier="confirmed_daily",
            session_date=date(2026, 11, 18),
            config=sample_config,
        )
        # Close t1
        t1.status = "closed"
        db_session.commit()

        # Cooldown is 3 days. Day 19 is within 3 days: should be suppressed
        t2 = record_paper_trade_entry(
            session=db_session,
            signal_id=2,
            symbol="WIPRO",
            direction="BUY",
            entry_price=505.0,
            risk_ref=risk_ref,
            confluence_result=confluence_res,
            alert_tier="confirmed_daily",
            session_date=date(2026, 11, 19),
            config=sample_config,
        )
        assert t2 is None


class TestPaperTradeMonitoringAndExits:
    def test_monitor_stop_hit(self, db_session, sample_config):
        notifier = DummyNotifier()
        risk_ref = {"status": "available", "stop_reference": 950.0, "target_reference": 1100.0, "risk_distance": 50.0}
        trade = record_paper_trade_entry(
            session=db_session,
            signal_id=1,
            symbol="TATAMOTORS",
            direction="BUY",
            entry_price=1000.0,
            risk_ref=risk_ref,
            confluence_result={"confluence_score": 2},
            alert_tier="confirmed_daily",
            session_date=date(2026, 11, 20),
            config=sample_config,
        )
        assert trade.status == "open"

        # Price drops to 940 (below stop 950)
        quotes = {"TATAMOTORS": {"last_price": 940.0}}
        monitor_open_paper_trades(
            session=db_session,
            quotes_by_symbol=quotes,
            notifiers=[notifier],
            config=sample_config,
            session_date=date(2026, 11, 21),
        )

        db_session.refresh(trade)
        assert trade.status == "closed"
        assert trade.exit_reason == "stop_hit"
        assert trade.exit_price == 940.0
        assert trade.pnl_absolute == -60.0
        assert trade.pnl_percent == -6.0
        assert trade.r_multiple == -1.2
        assert len(notifier.broadcasts) >= 1
        assert "Closed: Stop Hit" in notifier.broadcasts[-1]

    def test_monitor_target_hit(self, db_session, sample_config):
        notifier = DummyNotifier()
        risk_ref = {"status": "available", "stop_reference": 950.0, "target_reference": 1100.0, "risk_distance": 50.0}
        trade = record_paper_trade_entry(
            session=db_session,
            signal_id=1,
            symbol="SBIN",
            direction="BUY",
            entry_price=1000.0,
            risk_ref=risk_ref,
            confluence_result={"confluence_score": 2},
            alert_tier="confirmed_daily",
            session_date=date(2026, 11, 20),
            config=sample_config,
        )

        # Price reaches 1105 (above target 1100)
        quotes = {"SBIN": {"last_price": 1105.0}}
        monitor_open_paper_trades(
            session=db_session,
            quotes_by_symbol=quotes,
            notifiers=[notifier],
            config=sample_config,
            session_date=date(2026, 11, 21),
        )

        db_session.refresh(trade)
        assert trade.status == "closed"
        assert trade.exit_reason == "target_hit"
        assert trade.exit_price == 1105.0
        assert trade.pnl_absolute == 105.0
        assert trade.pnl_percent == 10.5
        assert trade.r_multiple == 2.1
        assert len(notifier.broadcasts) >= 1
        assert "Closed: Target Hit" in notifier.broadcasts[-1]

    def test_monitor_time_expiry(self, db_session, sample_config):
        notifier = DummyNotifier()
        risk_ref = {"status": "available", "stop_reference": 900.0, "target_reference": 1200.0, "risk_distance": 100.0}
        trade = record_paper_trade_entry(
            session=db_session,
            signal_id=1,
            symbol="HDFCBANK",
            direction="BUY",
            entry_price=1000.0,
            risk_ref=risk_ref,
            confluence_result={"confluence_score": 2},
            alert_tier="confirmed_daily",
            session_date=date(2026, 11, 1),
            config=sample_config,
        )

        # Check 15 days later (max_hold_days is 10)
        quotes = {"HDFCBANK": {"last_price": 1020.0}}
        monitor_open_paper_trades(
            session=db_session,
            quotes_by_symbol=quotes,
            notifiers=[notifier],
            config=sample_config,
            session_date=date(2026, 11, 16),
        )

        db_session.refresh(trade)
        assert trade.status == "closed"
        assert trade.exit_reason == "time_expiry"
        assert trade.exit_price == 1020.0
        assert trade.pnl_percent == 2.0


class TestMilestoneAlerts:
    def test_milestone_alerts_trigger_at_2_5_and_5_percent(self, db_session, sample_config):
        notifier = DummyNotifier()
        risk_ref = {"status": "available", "stop_reference": 900.0, "target_reference": 1200.0, "risk_distance": 100.0}
        trade = record_paper_trade_entry(
            session=db_session,
            signal_id=1,
            symbol="AXISBANK",
            direction="BUY",
            entry_price=1000.0,
            risk_ref=risk_ref,
            confluence_result={"confluence_score": 2},
            alert_tier="intraday_radar",
            session_date=date(2026, 11, 20),
            config=sample_config,
        )

        # Step 1: price rises to 1028 (+2.8% -> triggers +2.5% milestone)
        quotes = {"AXISBANK": {"last_price": 1028.0}}
        monitor_open_paper_trades(
            session=db_session,
            quotes_by_symbol=quotes,
            notifiers=[notifier],
            config=sample_config,
            session_date=date(2026, 11, 20),
        )

        milestones = db_session.query(PaperTradeMilestone).filter_by(trade_id=trade.id).all()
        assert len(milestones) == 1
        assert milestones[0].milestone_pct == 2.5
        assert milestones[0].notified is True
        assert any("+2.5%" in b for b in notifier.broadcasts)

        # Step 2: price stays at 1030 (+3.0%) -> should NOT fire +2.5% again
        monitor_open_paper_trades(
            session=db_session,
            quotes_by_symbol={"AXISBANK": {"last_price": 1030.0}},
            notifiers=[notifier],
            config=sample_config,
            session_date=date(2026, 11, 20),
        )
        assert db_session.query(PaperTradeMilestone).filter_by(trade_id=trade.id).count() == 1

        # Step 3: price rises to 1055 (+5.5% -> triggers +5.0% milestone)
        monitor_open_paper_trades(
            session=db_session,
            quotes_by_symbol={"AXISBANK": {"last_price": 1055.0}},
            notifiers=[notifier],
            config=sample_config,
            session_date=date(2026, 11, 20),
        )
        assert db_session.query(PaperTradeMilestone).filter_by(trade_id=trade.id).count() == 2
        pcts = {m.milestone_pct for m in db_session.query(PaperTradeMilestone).filter_by(trade_id=trade.id).all()}
        assert pcts == {2.5, 5.0}


class TestWeeklyReport:
    def test_weekly_report_compilation_and_storage(self, db_session, sample_config):
        notifier = DummyNotifier()
        week_start = date(2026, 11, 16)  # Monday
        week_end = date(2026, 11, 20)    # Friday

        # Trade 1: Winner (+4.0%, +1.6R, confirmed_daily, score 2)
        t1 = PaperTrade(
            symbol="STOCK1", direction="BUY", entry_price=100.0, entry_date=week_start,
            entry_datetime=datetime.now(timezone.utc), stop_loss=97.5, target_price=105.0,
            risk_distance=2.5, confluence_score=2, alert_tier="confirmed_daily",
            exit_price=104.0, exit_date=week_start + timedelta(days=2),
            exit_reason="target_hit", pnl_absolute=4.0, pnl_percent=4.0, r_multiple=1.6,
            status="closed", created_at=datetime.now(timezone.utc),
        )
        # Trade 2: Loser (-2.0%, -0.8R, intraday_radar, score 2)
        t2 = PaperTrade(
            symbol="STOCK2", direction="BUY", entry_price=200.0, entry_date=week_start + timedelta(days=1),
            entry_datetime=datetime.now(timezone.utc), stop_loss=195.0, target_price=210.0,
            risk_distance=5.0, confluence_score=2, alert_tier="intraday_radar",
            exit_price=196.0, exit_date=week_start + timedelta(days=3),
            exit_reason="stop_hit", pnl_absolute=-4.0, pnl_percent=-2.0, r_multiple=-0.8,
            status="closed", created_at=datetime.now(timezone.utc),
        )
        # Trade 3: Still open
        t3 = PaperTrade(
            symbol="STOCK3", direction="BUY", entry_price=300.0, entry_date=week_start + timedelta(days=2),
            entry_datetime=datetime.now(timezone.utc), stop_loss=290.0, target_price=320.0,
            risk_distance=10.0, confluence_score=3, alert_tier="confirmed_daily",
            status="open", created_at=datetime.now(timezone.utc),
        )
        db_session.add_all([t1, t2, t3])
        db_session.commit()

        report = generate_weekly_report(
            session=db_session,
            notifiers=[notifier],
            config=sample_config,
            week_start_date=week_start,
            week_end_date=week_end,
            send_notification=True,
        )

        assert report["closed_trades"] == 2
        assert report["open_trades"] == 1
        assert report["winners"] == 1
        assert report["losers"] == 1
        assert report["win_rate_pct"] == 50.0
        assert report["avg_pnl_pct"] == 1.0  # (4.0 - 2.0) / 2
        assert report["avg_r_multiple"] == 0.4  # (1.6 - 0.8) / 2
        assert report["best_trade"]["symbol"] == "STOCK1"
        assert report["worst_trade"]["symbol"] == "STOCK2"

        # Check DB persistence
        db_report = db_session.query(PaperTradeWeeklyReport).filter_by(week_start=week_start).first()
        assert db_report is not None
        assert db_report.win_rate_pct == 50.0
        assert db_report.closed_trades == 2
        assert len(notifier.broadcasts) >= 1
        assert "Weekly Paper Trading Report" in notifier.broadcasts[-1]
