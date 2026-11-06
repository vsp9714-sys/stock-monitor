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
import json
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy import event

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


class MarketContextCache(Base):
    """Small persistent cache for benchmark and sector index daily candles."""
    __tablename__ = "market_context_cache"

    context_key = Column(String(120), primary_key=True)
    candles_json = Column(Text, nullable=False)
    fetched_at = Column(DateTime, nullable=False)


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


class DailyCandleRefresh(Base):
    """Per-symbol checkpoint for post-close confirmation of a daily candle."""
    __tablename__ = "daily_candle_refresh"

    symbol = Column(String(30), primary_key=True)
    session_date = Column(Date, nullable=False)
    status = Column(String(20), nullable=False)  # confirmed, no_bar, failed
    updated_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))


class UniverseDataSnapshot(Base):
    """Last-known-good symbol data downloaded from an authoritative universe source."""
    __tablename__ = "universe_data_snapshots"

    id = Column(Integer, primary_key=True)
    source_key = Column(String(40), nullable=False, index=True)
    snapshot_date = Column(Date, nullable=False)
    source = Column(String(300), nullable=False)
    retrieved_at = Column(DateTime, nullable=False)
    symbols_json = Column(Text, nullable=False)
    item_count = Column(Integer, nullable=False)

    __table_args__ = (UniqueConstraint("source_key", "snapshot_date", name="uq_universe_source_snapshot_day"),)


class UniverseRefreshAttempt(Base):
    """One source refresh attempt per configured trading date, including failures."""
    __tablename__ = "universe_refresh_attempts"

    id = Column(Integer, primary_key=True)
    source_key = Column(String(40), nullable=False)
    attempt_date = Column(Date, nullable=False)
    attempted_at = Column(DateTime, nullable=False)
    status = Column(String(20), nullable=False)
    source_error = Column(Text)

    __table_args__ = (UniqueConstraint("source_key", "attempt_date", name="uq_universe_attempt_day"),)


class UniverseScanSnapshot(Base):
    """Dated selected-symbol set and provenance for reproducible scans/backtests."""
    __tablename__ = "universe_scan_snapshots"

    id = Column(Integer, primary_key=True)
    snapshot_date = Column(Date, nullable=False)
    mode = Column(String(40), nullable=False)
    created_at = Column(DateTime, nullable=False)
    metadata_json = Column(Text, nullable=False)
    symbols_json = Column(Text, nullable=False)


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


class SignalDelivery(Base):
    """Last confluence score successfully delivered to each notification target."""
    __tablename__ = "signal_deliveries"

    id = Column(Integer, primary_key=True)
    signal_id = Column(Integer, ForeignKey("signals.id", ondelete="CASCADE"), nullable=False, index=True)
    destination = Column(String(80), nullable=False)
    last_sent_score = Column(Integer, nullable=False, default=0)
    sent_at = Column(DateTime)

    __table_args__ = (
        UniqueConstraint("signal_id", "destination", name="uq_signal_delivery_destination"),
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
        _engine = create_sqlite_engine(db_url)
        Base.metadata.create_all(_engine)
        logger.info(f"Database initialized at {db_path.absolute()}")
    return _engine


def create_sqlite_engine(db_url: str):
    """Create a SQLite engine configured for concurrent scanner/cache access."""
    engine = create_engine(
        db_url,
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_busy_timeout(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    # WAL lets scanner readers proceed while a cache writer commits. SQLite
    # still serializes writers; the busy timeout lets short commits queue.
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
        connection.exec_driver_sql("PRAGMA synchronous=NORMAL")
    return engine


def prune_market_data_to_symbols(session: Session, symbols: set[str]) -> dict[str, int]:
    """Drop OHLCV, refresh checkpoints, and fundamentals outside a selected universe.

    Signal and delivery history plus the full instrument master are intentionally
    retained for auditability and for future constituent resolution.
    """
    normalized = {str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()}
    if not normalized:
        raise ValueError("Refusing to prune market data to an empty universe")

    counts = {}
    for name, model in (
        ("ohlcv_cache", OHLCVCache),
        ("daily_candle_refresh", DailyCandleRefresh),
        ("fundamentals", Fundamental),
    ):
        counts[name] = session.query(model).filter(~model.symbol.in_(normalized)).delete(
            synchronize_session=False
        )
    session.commit()
    return counts


def get_session() -> Session:
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine())
    return _SessionFactory()


# ─── Helpers ──────────────────────────────────────────────────────────────────

def upsert_signal(session: Session, symbol: str, signal_date: date, signal_type: str,
                  direction: str, price: float, indicator_value: float,
                  indicator_detail: str, chart_path: Optional[str] = None) -> Signal:
    """Create a daily signal or persist its latest highest confluence snapshot."""
    existing = (
        session.query(Signal)
        .filter_by(symbol=symbol, signal_date=signal_date, signal_type=signal_type)
        .first()
    )
    if existing:
        old_detail = _parse_indicator_detail(existing.indicator_detail)
        new_detail = _parse_indicator_detail(indicator_detail)
        old_score = int(existing.indicator_value or 0)
        old_version = int(old_detail.get("score_version", 1) or 1)
        new_version = int(new_detail.get("score_version", 1) or 1)
        migrated = None
        if old_version < 2 <= new_version:
            migrated = _derive_legacy_group_snapshot(old_detail, existing.direction)
            if migrated is not None:
                old_score = migrated["score"]
                # Delivery scores used to be raw indicator counts. Normalize
                # them with the exact same legacy snapshot before comparing
                # new group-score escalations.
                for delivery in session.query(SignalDelivery).filter_by(signal_id=existing.id):
                    delivery.last_sent_score = migrated["score"]
        new_score = int(indicator_value or 0)
        if new_score >= old_score or old_version < new_version:
            existing.direction = direction
            existing.price = price
            if new_score >= old_score:
                existing.indicator_value = indicator_value
                existing.indicator_detail = indicator_detail
            else:
                # Store the legacy snapshot on the new group-score scale; this
                # avoids treating a lower group score as a regression from a
                # larger raw-check count on the next scan.
                old_detail.update({
                    "score_version": new_version,
                    "confluence_score": old_score,
                    "total_signals": migrated["total_groups"] if migrated else old_detail.get("total_signals", 0),
                    "group_votes": migrated["group_votes"] if migrated else {},
                    "confluence_mode": "legacy-score-migrated",
                    "legacy_score_migrated": True,
                })
                existing.indicator_value = float(old_score)
                existing.indicator_detail = json.dumps(old_detail, separators=(",", ":"))
            existing.chart_path = chart_path
            if new_score > old_score:
                existing.notified = False
                existing.notified_at = None
            session.commit()
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


def _parse_indicator_detail(value: Optional[str]) -> dict:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _derive_legacy_group_snapshot(detail: dict, direction: Optional[str]) -> Optional[dict]:
    """Convert a pre-v2 raw-check snapshot to the new independent-group scale."""
    checks = detail.get("checks")
    if not isinstance(checks, list):
        return None
    groups: dict[str, set[str]] = {}
    for check in checks:
        if not isinstance(check, dict) or not check.get("met"):
            continue
        check_direction = check.get("direction")
        if check_direction not in {"BUY", "SELL"}:
            continue
        signal_type = str(check.get("signal_type", ""))
        if signal_type.startswith("ema_"):
            group = "trend"
        elif signal_type in {"rsi", "macd"}:
            group = "momentum"
        elif signal_type == "volume_breakout":
            group = "participation"
        else:
            continue
        groups.setdefault(group, set()).add(check_direction)
    if not groups:
        return None
    votes = {
        group: next(iter(directions))
        for group, directions in groups.items()
        if len(directions) == 1
    }
    return {
        "score": sum(1 for vote in votes.values() if vote == direction),
        "total_groups": len(groups),
        "group_votes": votes,
    }


def mark_signal_notified(session: Session, signal_id: int) -> None:
    """Mark a signal as notified."""
    sig = session.get(Signal, signal_id)
    if sig:
        sig.notified = True
        sig.notified_at = datetime.now(timezone.utc)
        session.commit()


def get_delivery_score(session: Session, signal_id: int, destination: str) -> Optional[int]:
    delivery = session.query(SignalDelivery).filter_by(
        signal_id=signal_id, destination=destination
    ).first()
    return delivery.last_sent_score if delivery else None


def mark_delivery_sent(session: Session, signal_id: int, destination: str, score: int) -> None:
    """Record a successful escalation delivery for one recipient/channel."""
    delivery = session.query(SignalDelivery).filter_by(
        signal_id=signal_id, destination=destination
    ).first()
    if delivery is None:
        delivery = SignalDelivery(
            signal_id=signal_id,
            destination=destination,
            last_sent_score=score,
            sent_at=datetime.now(timezone.utc),
        )
        session.add(delivery)
    elif score > delivery.last_sent_score:
        delivery.last_sent_score = score
        delivery.sent_at = datetime.now(timezone.utc)
    session.commit()


def seed_legacy_deliveries(session: Session, destinations: list[str]) -> None:
    """Prevent rollout duplicates for old destinations, but not newly-added group chats."""
    legacy_signals = session.query(Signal).filter(
        Signal.notified.is_(True), Signal.signal_type.like("confluence_%")
    ).all()
    # The previous implementation could only address the configured personal
    # chat. A newly enabled group has never received these historical alerts.
    legacy_destinations = [target for target in destinations if target != "telegram:group"]
    for sig in legacy_signals:
        detail = _parse_indicator_detail(sig.indicator_detail)
        migrated = _derive_legacy_group_snapshot(detail, sig.direction)
        score = migrated["score"] if migrated is not None else int(sig.indicator_value or 0)
        for destination in legacy_destinations:
            if get_delivery_score(session, sig.id, destination) is None:
                mark_delivery_sent(session, sig.id, destination, score)


def refresh_signal_notified_state(session: Session, signal_id: int, destinations: list[str]) -> None:
    """Keep legacy notified flag true only when every configured target got latest score."""
    sig = session.get(Signal, signal_id)
    if not sig:
        return
    score = int(sig.indicator_value or 0)
    delivered = bool(destinations) and all(
        (get_delivery_score(session, signal_id, destination) or 0) >= score
        for destination in destinations
    )
    sig.notified = delivered
    if delivered:
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


def upsert_ohlcv_rows(session: Session, rows: list[dict], *, commit: bool = True) -> int:
    """Insert/update OHLCV rows idempotently using the cache's unique key."""
    if not rows:
        return 0
    # Stay below SQLite's bind-variable limit on older system packages.
    # 100 rows × 7 values stays under SQLite's historical 999-variable limit.
    for offset in range(0, len(rows), 100):
        statement = sqlite_insert(OHLCVCache).values(rows[offset:offset + 100])
        statement = statement.on_conflict_do_update(
            index_elements=["symbol", "interval", "ts"],
            set_={
                "open": statement.excluded.open,
                "high": statement.excluded.high,
                "low": statement.excluded.low,
                "close": statement.excluded.close,
                "volume": statement.excluded.volume,
            },
        )
        session.execute(statement)
    if commit:
        session.commit()
    return len(rows)
