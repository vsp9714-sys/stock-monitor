"""
Database layer — SQLite via SQLAlchemy.
Manages:
  - stocks         : master list of all NSE/BSE instruments
  - ohlcv_cache    : recent OHLCV candles (rolling window)
  - fundamentals   : company fundamentals cache (mcap, profit, category)
  - signals        : detected signals (deduped per stock per day)
  - signal_log     : full historical signal log
"""
import logging
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from src.config_loader import get_config

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


class Stock(Base):
    """NSE/BSE instrument master list."""
    __tablename__ = "stocks"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(30), unique=True, nullable=False, index=True)
    isin = Column(String(20), index=True)
    company_name = Column(String(200))
    exchange = Column(String(10))          # NSE / BSE
    instrument_key = Column(String(50))    # Upstox instrument key
    series = Column(String(10))            # EQ, BE, etc.
    is_active = Column(Boolean, default=True)
    last_updated = Column(DateTime)


class Fundamental(Base):
    """Company fundamentals — cached from Screener.in."""
    __tablename__ = "fundamentals"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(30), nullable=False, index=True)
    isin = Column(String(20))
    mcap_cr = Column(Float)                # Market cap in Crores
    cap_category = Column(String(20))      # large / mid / small / micro
    sector = Column(String(100))
    industry = Column(String(100))
    pe_ratio = Column(Float)
    pb_ratio = Column(Float)
    roe = Column(Float)                    # Return on equity %
    # Last 4 quarters net profit (Q1, Q2, Q3, Q4 from latest)
    profit_q1_cr = Column(Float)
    profit_q2_cr = Column(Float)
    profit_q3_cr = Column(Float)
    profit_q4_cr = Column(Float)
    profit_ttm_cr = Column(Float)          # Trailing twelve months profit
    revenue_ttm_cr = Column(Float)
    debt_to_equity = Column(Float)
    promoter_holding_pct = Column(Float)
    screener_url = Column(String(300))
    fetched_at = Column(DateTime)

    __table_args__ = (UniqueConstraint("symbol", name="uq_fundamentals_symbol"),)


class OHLCVCache(Base):
    """Rolling OHLCV candle cache — keeps last N candles per symbol per interval."""
    __tablename__ = "ohlcv_cache"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(30), nullable=False, index=True)
    interval = Column(String(10), nullable=False)   # 15m, 1d, etc.
    ts = Column(DateTime, nullable=False)
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    volume = Column(Float)

    __table_args__ = (
        UniqueConstraint("symbol", "interval", "ts", name="uq_ohlcv_symbol_interval_ts"),
    )


class Signal(Base):
    """Detected trading signals — deduped per symbol per signal_type per day."""
    __tablename__ = "signals"

    id = Column(Integer, primary_key=True)
    symbol = Column(String(30), nullable=False, index=True)
    signal_date = Column(Date, nullable=False, index=True)
    signal_type = Column(String(50), nullable=False)   # rsi_oversold, macd_bullish, etc.
    direction = Column(String(10))                      # BUY / SELL / NEUTRAL
    price = Column(Float)
    indicator_value = Column(Float)                     # e.g., RSI=28.5
    indicator_detail = Column(Text)                     # JSON with full indicator snapshot
    chart_path = Column(String(300))                    # local path to chart image
    notified = Column(Boolean, default=False)
    notified_at = Column(DateTime)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        UniqueConstraint(
            "symbol", "signal_date", "signal_type",
            name="uq_signal_symbol_date_type"
        ),
    )


# ─── Engine & Session ─────────────────────────────────────────────────────────

_engine = None
_SessionFactory = None


def get_engine():
    global _engine
    if _engine is None:
        cfg = get_config()
        db_path = Path(cfg["database"]["path"])
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_url = f"sqlite:///{db_path.absolute()}"
        _engine = create_engine(db_url, connect_args={"check_same_thread": False})
        Base.metadata.create_all(_engine)
        logger.info(f"Database initialized at {db_path.absolute()}")
    return _engine


def get_session() -> Session:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine())
    return _SessionFactory()


# ─── Helpers ──────────────────────────────────────────────────────────────────

def upsert_signal(session: Session, symbol: str, signal_date: date, signal_type: str,
                  direction: str, price: float, indicator_value: float,
                  indicator_detail: str, chart_path: Optional[str] = None) -> Signal:
    """Insert or ignore a signal (deduplicated per symbol+date+type)."""
    existing = (
        session.query(Signal)
        .filter_by(symbol=symbol, signal_date=signal_date, signal_type=signal_type)
        .first()
    )
    if existing:
        return existing

    sig = Signal(
        symbol=symbol,
        signal_date=signal_date,
        signal_type=signal_type,
        direction=direction,
        price=price,
        indicator_value=indicator_value,
        indicator_detail=indicator_detail,
        chart_path=chart_path,
        created_at=datetime.now(timezone.utc),
    )
    session.add(sig)
    session.commit()
    return sig


def mark_signal_notified(session: Session, signal_id: int) -> None:
    """Mark a signal as notified."""
    sig = session.get(Signal, signal_id)
    if sig:
        sig.notified = True
        sig.notified_at = datetime.now(timezone.utc)
        session.commit()


def get_pending_signals(session: Session) -> list[Signal]:
    """Get all signals that haven't been notified yet."""
    return session.query(Signal).filter_by(notified=False).all()


def prune_ohlcv_cache(session: Session, symbol: str, interval: str, keep_rows: int = 500) -> None:
    """Keep only the last N candles for a symbol+interval pair."""
    subq = (
        session.query(OHLCVCache.id)
        .filter_by(symbol=symbol, interval=interval)
        .order_by(OHLCVCache.ts.desc())
        .limit(keep_rows)
        .subquery()
    )
    session.query(OHLCVCache).filter(
        OHLCVCache.symbol == symbol,
        OHLCVCache.interval == interval,
        ~OHLCVCache.id.in_(subq),
    ).delete(synchronize_session=False)
    session.commit()
