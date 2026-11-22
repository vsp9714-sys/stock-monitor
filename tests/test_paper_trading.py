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

        # Second attempt on same session date and same tier should not create a second row
        t2 = record_paper_trade_entry(
            session=db_session,
            signal_id=2,
            symbol="INFY",
            direction="BUY",
            entry_price=1505.0,
            risk_ref=risk_ref,
            confluence_result=confluence_res,
            alert_tier="intraday_radar",
            session_date=date(2026, 11, 20),
            config=sample_config,
        )
        assert t2.id == t1.id
        assert db_session.query(PaperTrade).filter_by(symbol="INFY").count() == 1

    def test_record_paper_trade_tier_isolation(self, db_session, sample_config):
        """Radar (10:30) and Power-Hour Confirmed (15:15) maintain separate virtual books."""
        risk_ref = {"status": "available", "stop_reference": 980.0, "target_reference": 1040.0}
        confluence_res = {"confluence_score": 2}
        t_radar = record_paper_trade_entry(
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
        assert t_radar is not None

        t_confirmed = record_paper_trade_entry(
            session=db_session,
            signal_id=2,
            symbol="INFY",
            direction="BUY",
            entry_price=1505.0,
            risk_ref=risk_ref,
            confluence_result=confluence_res,
            alert_tier="power_hour_confirmed",
            session_date=date(2026, 11, 20),
            config=sample_config,
        )
        assert t_confirmed is not None
        assert t_confirmed.id != t_radar.id
        assert t_radar.alert_type == "intraday"
        assert t_confirmed.alert_type == "day_end"
        assert db_session.query(PaperTrade).filter_by(symbol="INFY").count() == 2

        # Second intraday_radar on same day is deduplicated
        t_radar_dup = record_paper_trade_entry(
            session=db_session,
            signal_id=3,
            symbol="INFY",
            direction="BUY",
            entry_price=1502.0,
            risk_ref=risk_ref,
            confluence_result=confluence_res,
            alert_tier="intraday_radar",
            session_date=date(2026, 11, 20),
            config=sample_config,
        )
        assert t_radar_dup.id == t_radar.id
        assert db_session.query(PaperTrade).filter_by(symbol="INFY").count() == 2

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


class TestPhase1AndPhase2PaperTradingEnhancements:
    def test_main_weekly_report_job_no_name_error(self, monkeypatch):
        """BUG-1: generate_weekly_report_job in src.main must not raise NameError for get_session."""
        from types import SimpleNamespace
        import src.main as main_mod
        from src.db import Base, create_sqlite_engine
        from sqlalchemy.orm import sessionmaker

        engine = create_sqlite_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        TestSession = sessionmaker(bind=engine)

        monkeypatch.setattr(main_mod, "get_session", lambda: TestSession())
        monkeypatch.setattr(main_mod, "scanner", SimpleNamespace(notifiers=[], cfg={"paper_trading": {"enabled": True}}))

        # Should execute cleanly without NameError
        main_mod.generate_weekly_report_job()

    def test_notifiers_broadcast_message_enabled_attribute(self):
        """BUG-8: DiscordNotifier and EmailNotifier must have self.enabled attribute."""
        from src.notifiers.discord import DiscordNotifier
        from src.notifiers.email_notifier import EmailNotifier

        dn = DiscordNotifier({"notifications": {"discord": {"enabled": False, "webhook_url": "https://discord.com/api/webhooks/test"}}})
        assert hasattr(dn, "enabled")
        assert dn.enabled is False
        assert dn.broadcast_message("test") is False

        en = EmailNotifier({"notifications": {"email": {"enabled": False, "username": "test_user", "password": "test_password", "to_addrs": ["to@example.com"]}}})
        assert hasattr(en, "enabled")
        assert en.enabled is False
        assert en.broadcast_message("test") is False

    def test_trading_days_between_nse_calendar(self):
        """trading_days_between must count only NSE trading sessions, excluding weekends and holidays."""
        from src.paper_trading import trading_days_between

        # 2026-11-06 (Fri) to 2026-11-09 (Mon) -> 1 trading session (Monday)
        # Note: 2026-11-10 is Diwali Balipratipada (holiday in config/nse_trading_calendar.yaml)
        # 2026-11-09 to 2026-11-11 -> Nov 10 is holiday, Nov 11 is trading day -> exactly 1 session!
        d_mon = date(2026, 11, 9)
        d_wed = date(2026, 11, 11)
        count = trading_days_between(d_mon, d_wed)
        assert count == 1  # only Nov 11 is a trading day

    def test_allowed_directions_skips_sell(self, db_session, sample_config):
        """Long-only validation: when allowed_directions is ['BUY'], SELL alerts return None."""
        sample_config["paper_trading"]["allowed_directions"] = ["BUY"]
        res_sell = record_paper_trade_entry(
            session=db_session,
            signal_id=1,
            symbol="INFY",
            direction="SELL",
            entry_price=1500.0,
            risk_ref={"status": "available", "stop_reference": 1530.0, "target_reference": 1440.0},
            confluence_result={"confluence_score": 2},
            alert_tier="confirmed_daily",
            session_date=date(2026, 11, 20),
            config=sample_config,
        )
        assert res_sell is None
        assert db_session.query(PaperTrade).filter_by(symbol="INFY").count() == 0

    def test_intraday_high_low_stop_first_exit(self, db_session, sample_config):
        """On subsequent days, if quote['low'] <= stop_loss, close as stop_hit even if last_price > stop."""
        trade = record_paper_trade_entry(
            session=db_session,
            signal_id=1,
            symbol="BHARTIARTL",
            direction="BUY",
            entry_price=1000.0,
            risk_ref={"status": "available", "stop_reference": 950.0, "target_reference": 1100.0, "risk_distance": 50.0},
            confluence_result={"confluence_score": 2},
            alert_tier="confirmed_daily",
            session_date=date(2026, 11, 20),
            config=sample_config,
        )

        # Subsequent session: open 980, low 940 (hits stop), high 1020, last_price 1010
        quotes = {"BHARTIARTL": {
            "open": 980.0, "high": 1020.0, "low": 940.0, "last_price": 1010.0
        }}
        monitor_open_paper_trades(
            session=db_session,
            quotes_by_symbol=quotes,
            notifiers=[],
            config=sample_config,
            session_date=date(2026, 11, 21),
        )

        db_session.refresh(trade)
        assert trade.status == "closed"
        assert trade.exit_reason == "stop_hit"
        # Since open (980) > stop (950), fill is at stop_loss (950)
        assert trade.exit_price == 950.0
        assert trade.max_favorable_excursion == 1020.0
        assert trade.max_adverse_excursion == 940.0

    def test_cost_deduction_and_net_r_multiple(self, db_session, sample_config):
        """round_trip_cost_pct (e.g. 0.30%) reduces net pnl_percent and net_r_multiple."""
        sample_config["paper_trading"]["round_trip_cost_pct"] = 0.30
        trade = record_paper_trade_entry(
            session=db_session,
            signal_id=1,
            symbol="ITC",
            direction="BUY",
            entry_price=400.0,
            risk_ref={"status": "available", "stop_reference": 380.0, "target_reference": 440.0, "risk_distance": 20.0},
            confluence_result={"confluence_score": 2},
            alert_tier="confirmed_daily",
            session_date=date(2026, 11, 20),
            config=sample_config,
        )

        # Target hit at 440 (+10% gross)
        quotes = {"ITC": {"open": 420.0, "high": 445.0, "low": 415.0, "last_price": 440.0}}
        monitor_open_paper_trades(
            session=db_session,
            quotes_by_symbol=quotes,
            notifiers=[],
            config=sample_config,
            session_date=date(2026, 11, 21),
        )

        db_session.refresh(trade)
        assert trade.status == "closed"
        assert trade.exit_reason == "target_hit"
        assert trade.exit_price == 440.0
        assert trade.gross_pnl_percent == 10.0
        assert trade.cost_percent == 0.30
        assert trade.pnl_percent == 9.70
        assert trade.r_multiple == 2.0
        # risk_distance_pct = 20 / 400 * 100 = 5%
        # net_r_multiple = 9.70 / 5.0 = 1.94
        assert trade.net_r_multiple == 1.94

    def test_weekly_report_profit_factor_expectancy_and_direction_split(self, db_session, sample_config):
        """Weekly report includes profit_factor, expectancy_r, and direction_stats."""
        week_start = date(2026, 11, 16)
        week_end = date(2026, 11, 20)

        # Closed BUY trade (+10% net, +2.0R)
        t_buy = PaperTrade(
            symbol="BUY1", direction="BUY", entry_price=100.0, entry_date=week_start,
            entry_datetime=datetime.now(timezone.utc), stop_loss=95.0, target_price=110.0,
            risk_distance=5.0, confluence_score=2, alert_tier="confirmed_daily",
            exit_price=110.0, exit_date=week_start + timedelta(days=2),
            exit_reason="target_hit", pnl_absolute=10.0, gross_pnl_percent=10.0,
            cost_percent=0.30, pnl_percent=9.70, r_multiple=2.0, net_r_multiple=1.94,
            status="closed", created_at=datetime.now(timezone.utc),
        )
        # Closed SELL trade (-5% net, -1.0R)
        t_sell = PaperTrade(
            symbol="SELL1", direction="SELL", entry_price=100.0, entry_date=week_start,
            entry_datetime=datetime.now(timezone.utc), stop_loss=105.0, target_price=90.0,
            risk_distance=5.0, confluence_score=2, alert_tier="confirmed_daily",
            exit_price=105.0, exit_date=week_start + timedelta(days=3),
            exit_reason="stop_hit", pnl_absolute=-5.0, gross_pnl_percent=-5.0,
            cost_percent=0.30, pnl_percent=-5.30, r_multiple=-1.0, net_r_multiple=-1.06,
            status="closed", created_at=datetime.now(timezone.utc),
        )
        db_session.add_all([t_buy, t_sell])
        db_session.commit()

        rep = generate_weekly_report(
            session=db_session,
            notifiers=[],
            config=sample_config,
            week_start_date=week_start,
            week_end_date=week_end,
            send_notification=False,
        )

        assert rep["closed_trades"] == 2
        assert rep["profit_factor"] == round(9.70 / 5.30, 2)
        assert rep["expectancy_r"] == round((1.94 - 1.06) / 2, 2)
        assert rep["direction_stats"]["buy_trades"] == 1
        assert rep["direction_stats"]["buy_win_rate"] == 100.0
        assert rep["direction_stats"]["sell_trades"] == 1
        assert rep["direction_stats"]["sell_win_rate"] == 0.0

        db_rep = db_session.query(PaperTradeWeeklyReport).filter_by(week_start=week_start).first()
        assert db_rep.profit_factor == rep["profit_factor"]
        assert db_rep.expectancy_r == rep["expectancy_r"]
        assert db_rep.buy_trades == 1
        assert db_rep.sell_trades == 1

    def test_entry_day_phantom_fill_guard(self, db_session, sample_config):
        """On cur_date == entry_date, quote['low'] <= stop_loss with last_price > stop_loss must NOT close the trade."""
        trade = record_paper_trade_entry(
            session=db_session,
            signal_id=1,
            symbol="TCS",
            direction="BUY",
            entry_price=3500.0,
            risk_ref={"status": "available", "stop_reference": 3400.0, "target_reference": 3700.0, "risk_distance": 100.0},
            confluence_result={"confluence_score": 2},
            alert_tier="confirmed_daily",
            session_date=date(2026, 11, 20),
            config=sample_config,
        )
        assert trade is not None
        # On same date, an earlier dip had low=3350, but current last_price is 3520 (above stop)
        quotes = {"TCS": {"open": 3480.0, "high": 3550.0, "low": 3350.0, "last_price": 3520.0}}
        monitor_open_paper_trades(
            session=db_session,
            quotes_by_symbol=quotes,
            notifiers=[],
            config=sample_config,
            session_date=date(2026, 11, 20),
        )
        db_session.refresh(trade)
        assert trade.status == "open"
        assert trade.exit_reason is None

    def test_cooldown_checks_exit_date_not_entry_date(self, db_session, sample_config):
        """Trade entered 15 days ago, exited 1 trading day ago -> suppressed by 3-day cooldown."""
        sample_config["paper_trading"]["cooldown_days"] = 3
        # Trade entered 15 days ago, exited on 2026-11-19
        old_trade = PaperTrade(
            symbol="RELIANCE",
            direction="BUY",
            entry_price=2400.0,
            entry_date=date(2026, 11, 5),
            entry_datetime=datetime.now(timezone.utc),
            stop_loss=2300.0,
            target_price=2600.0,
            alert_tier="confirmed_daily",
            status="closed",
            exit_date=date(2026, 11, 19),
            exit_price=2600.0,
            exit_reason="target_hit",
            pnl_percent=8.33,
        )
        db_session.add(old_trade)
        db_session.commit()

        # New signal on 2026-11-20 (only 1 trading session after exit_date)
        new_trade = record_paper_trade_entry(
            session=db_session,
            signal_id=2,
            symbol="RELIANCE",
            direction="BUY",
            entry_price=2620.0,
            risk_ref={"status": "available", "stop_reference": 2550.0, "target_reference": 2750.0},
            confluence_result={"confluence_score": 2},
            alert_tier="confirmed_daily",
            session_date=date(2026, 11, 20),
            config=sample_config,
        )
        assert new_trade is None  # Suppressed because exit_date was only 1 session ago

    def test_open_paper_trade_unioned_into_quote_fetch_and_monitored(self, monkeypatch):
        """Open PaperTrade symbol excluded from universe is unioned into quote fetch and monitored in run_scan."""
        import pandas as pd
        from src.scanner import Scanner
        from src.db import Base, create_sqlite_engine, Stock, PaperTrade
        from sqlalchemy.orm import sessionmaker

        engine = create_sqlite_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        db = Session()

        stock_in = Stock(symbol="IN_UNIV", company_name="In Universe", instrument_key="NSE_EQ|IN", is_active=True)
        stock_out = Stock(symbol="OUT_UNIV", company_name="Out Universe", instrument_key="NSE_EQ|OUT", is_active=True)
        db.add_all([stock_in, stock_out])

        # Create open paper trade for OUT_UNIV
        pt_out = PaperTrade(
            symbol="OUT_UNIV",
            direction="BUY",
            entry_price=100.0,
            entry_date=date(2026, 11, 19),
            entry_datetime=datetime.now(timezone.utc),
            stop_loss=90.0,
            target_price=120.0,
            status="open",
            alert_tier="confirmed_daily",
        )
        db.add(pt_out)
        db.commit()

        from zoneinfo import ZoneInfo
        now_dt = datetime(2026, 11, 20, 15, 30, tzinfo=ZoneInfo("Asia/Kolkata"))
        now_ts = int(now_dt.timestamp() * 1000)

        # Mock fetcher
        fetched_keys = []
        class FakeFetcher:
            def get_full_market_quotes(self, keys, **kwargs):
                fetched_keys.extend(keys)
                return {
                    "NSE_EQ|IN": {
                        "last_price": 500.0, "open": 490.0, "high": 510.0, "low": 485.0,
                        "close": 500.0, "prev_close_price": 495.0, "volume": 100000,
                        "last_trade_time": str(now_ts), "ts": str(now_ts),
                    },
                    "NSE_EQ|OUT": {
                        "last_price": 125.0, "open": 115.0, "high": 126.0, "low": 114.0,
                        "close": 125.0, "prev_close_price": 110.0, "volume": 50000,
                        "last_trade_time": str(now_ts), "ts": str(now_ts),
                    },
                }

        scanner = object.__new__(Scanner)
        scanner.cfg = {
            "upstox": {"quote_batch_size": 100, "max_quote_age_seconds": 999999},
            "scan": {"min_coverage": 0.0},
            "universe": {"tradability": {"enabled": False}},
            "signals": {}, "guardrails": {}, "confluence": {},
            "paper_trading": {"enabled": True},
        }
        scanner.paper_trading_cfg = scanner.cfg["paper_trading"]
        scanner.universe_cfg = scanner.cfg["universe"]
        scanner.scan_cfg = scanner.cfg["scan"]
        scanner.signal_cfg = scanner.cfg["signals"]
        scanner.guardrails_cfg = scanner.cfg["guardrails"]
        scanner.confluence_cfg = scanner.cfg["confluence"]
        scanner.fetcher = FakeFetcher()
        scanner.notifiers = []
        scanner.get_active_stocks = lambda limit=None: [stock_in]  # Only stock_in is in universe!
        scanner.required_daily_candles = lambda: 30
        scanner._load_cached_daily_frames = lambda stocks, db: {
            "IN_UNIV": pd.DataFrame([{
                "ts": pd.Timestamp("2026-10-01") + pd.Timedelta(days=i),
                "open": 500, "high": 510, "low": 490, "close": 500, "volume": 10000,
            } for i in range(35)])
        }
        # Provide a fresh session factory for Scanner.run_scan
        monkeypatch.setattr("src.scanner.get_session", lambda: Session())
        monkeypatch.setattr("src.scanner.is_market_open", lambda *args, **kwargs: True)

        summary = scanner.run_scan(now=now_dt)

        # 1. Assert NSE_EQ|OUT was fetched
        assert "NSE_EQ|OUT" in fetched_keys
        # 2. Assert OUT_UNIV was monitored and closed (target_price was 120, quote high was 126, last 125)
        with Session() as check_sess:
            pt_refreshed = check_sess.query(PaperTrade).filter_by(symbol="OUT_UNIV").first()
            assert pt_refreshed is not None
            assert pt_refreshed.status == "closed"
            assert pt_refreshed.exit_reason == "target_hit"
