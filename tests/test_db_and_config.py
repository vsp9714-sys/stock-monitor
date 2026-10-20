"""
Tests for DB operations, deduplication, and config loading.
"""
from datetime import date, datetime, timezone
import os
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.config_loader import load_config, get_config
from src.db import Base, Stock, Signal, upsert_signal


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
