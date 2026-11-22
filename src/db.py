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


class PaperTrade(Base):
    """Auto-created paper trade entry when a confluence alert fires."""
    __tablename__ = "paper_trades"

    id = Column(Integer, primary_key=True)
    signal_id = Column(Integer, ForeignKey("signals.id", ondelete="SET NULL"), nullable=True, index=True)
    symbol = Column(String(30), nullable=False, index=True)
    direction = Column(String(10), nullable=False)  # BUY or SELL
    entry_price = Column(Float, nullable=False)
    entry_date = Column(Date, nullable=False, index=True)
    entry_datetime = Column(DateTime, nullable=False)

    # Risk references
    stop_loss = Column(Float, nullable=False)
    target_price = Column(Float, nullable=False)
    atr_at_entry = Column(Float)
    risk_distance = Column(Float)

    # Signal & confluence metadata
    confluence_score = Column(Integer)
    alert_tier = Column(String(30))
    alert_type = Column(String(20), nullable=False, default="day_end", index=True)
    rs_rating_at_entry = Column(Integer, nullable=True)
    signal_groups_json = Column(Text)

    # Excursion tracking
    max_favorable_excursion = Column(Float)
    max_adverse_excursion = Column(Float)

    # Exit tracking
    exit_price = Column(Float)
    exit_date = Column(Date)
    exit_datetime = Column(DateTime)
    exit_reason = Column(String(30))  # stop_hit, target_hit, time_expiry, manual
    pnl_absolute = Column(Float)
    pnl_percent = Column(Float)
    gross_pnl_percent = Column(Float)
    cost_percent = Column(Float)
    r_multiple = Column(Float)
    net_r_multiple = Column(Float)
    fits_tranche_rules = Column(Boolean)
    strategy_version = Column(String(30))

    # Lifecycle state
    status = Column(String(10), nullable=False, default="open", index=True)  # open, closed
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime)

    __table_args__ = (
        UniqueConstraint("symbol", "direction", "entry_date", "alert_type", name="uq_paper_trade_symbol_dir_date_type"),
    )


class PaperTradeMilestone(Base):
    """P&L milestone hit records — e.g. ±2.5%, ±5%."""
    __tablename__ = "paper_trade_milestones"

    id = Column(Integer, primary_key=True)
    trade_id = Column(Integer, ForeignKey("paper_trades.id", ondelete="CASCADE"), nullable=False, index=True)
    milestone_pct = Column(Float, nullable=False)
    hit_price = Column(Float, nullable=False)
    hit_datetime = Column(DateTime, nullable=False)
    notified = Column(Boolean, default=False)
    notified_at = Column(DateTime)

    __table_args__ = (
        UniqueConstraint("trade_id", "milestone_pct", name="uq_milestone_trade_pct"),
    )


class PaperTradeWeeklyReport(Base):
    """Aggregated weekly paper trade performance snapshot."""
    __tablename__ = "paper_trade_weekly_reports"

    id = Column(Integer, primary_key=True)
    week_start = Column(Date, nullable=False, unique=True, index=True)
    week_end = Column(Date, nullable=False)
    total_trades = Column(Integer, default=0)
    open_trades = Column(Integer, default=0)
    closed_trades = Column(Integer, default=0)
    winners = Column(Integer, default=0)
    losers = Column(Integer, default=0)
    win_rate_pct = Column(Float)
    avg_pnl_pct = Column(Float)
    avg_r_multiple = Column(Float)
    profit_factor = Column(Float)
    expectancy_r = Column(Float)
    buy_trades = Column(Integer, default=0)
    buy_win_rate = Column(Float)
    sell_trades = Column(Integer, default=0)
    sell_win_rate = Column(Float)
    best_trade_symbol = Column(String(30))
    best_trade_pnl_pct = Column(Float)
    worst_trade_symbol = Column(String(30))
    worst_trade_pnl_pct = Column(Float)
    radar_trades = Column(Integer, default=0)
    radar_win_rate = Column(Float)
    confirmed_trades = Column(Integer, default=0)
    confirmed_win_rate = Column(Float)
    score_2_trades = Column(Integer, default=0)
    score_2_win_rate = Column(Float)
    score_3_trades = Column(Integer, default=0)
    score_3_win_rate = Column(Float)
    report_json = Column(Text)
    generated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


# ─── Schema Migrations ────────────────────────────────────────────────────────

def ensure_schema_migrations(engine):
    """Ensure new columns and updated unique constraints exist on existing SQLite databases."""
    from sqlalchemy import text
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        pt_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(paper_trades)")).fetchall()}
        if pt_cols:
            if "gross_pnl_percent" not in pt_cols:
                conn.execute(text("ALTER TABLE paper_trades ADD COLUMN gross_pnl_percent REAL"))
            if "cost_percent" not in pt_cols:
                conn.execute(text("ALTER TABLE paper_trades ADD COLUMN cost_percent REAL"))
            if "net_r_multiple" not in pt_cols:
                conn.execute(text("ALTER TABLE paper_trades ADD COLUMN net_r_multiple REAL"))
            if "fits_tranche_rules" not in pt_cols:
                conn.execute(text("ALTER TABLE paper_trades ADD COLUMN fits_tranche_rules BOOLEAN"))
            if "strategy_version" not in pt_cols:
                conn.execute(text("ALTER TABLE paper_trades ADD COLUMN strategy_version VARCHAR(30)"))
            if "rs_rating_at_entry" not in pt_cols:
                conn.execute(text("ALTER TABLE paper_trades ADD COLUMN rs_rating_at_entry INTEGER"))

        wr_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(paper_trade_weekly_reports)")).fetchall()}
        if wr_cols:
            if "profit_factor" not in wr_cols:
                conn.execute(text("ALTER TABLE paper_trade_weekly_reports ADD COLUMN profit_factor REAL"))
            if "expectancy_r" not in wr_cols:
                conn.execute(text("ALTER TABLE paper_trade_weekly_reports ADD COLUMN expectancy_r REAL"))
            if "buy_trades" not in wr_cols:
                conn.execute(text("ALTER TABLE paper_trade_weekly_reports ADD COLUMN buy_trades INTEGER DEFAULT 0"))
            if "buy_win_rate" not in wr_cols:
                conn.execute(text("ALTER TABLE paper_trade_weekly_reports ADD COLUMN buy_win_rate REAL"))
            if "sell_trades" not in wr_cols:
                conn.execute(text("ALTER TABLE paper_trade_weekly_reports ADD COLUMN sell_trades INTEGER DEFAULT 0"))
            if "sell_win_rate" not in wr_cols:
                conn.execute(text("ALTER TABLE paper_trade_weekly_reports ADD COLUMN sell_win_rate REAL"))

        # Check unique constraint on paper_trades: upgrade if legacy constraint exists without alert_type
        res = conn.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name='paper_trades'")).fetchone()
        if res and res[0]:
            table_sql = res[0]
            if "uq_paper_trade_symbol_dir_date_type" not in table_sql:
                logger.info("Migrating paper_trades unique constraint to include alert_type...")
                # 1. Disable FKs outside a transaction in autocommit mode so SQLite does not rewrite child FKs
                conn.execute(text("PRAGMA foreign_keys=OFF"))
                conn.execute(text("BEGIN TRANSACTION"))
                try:
                    conn.execute(text("""
                        CREATE TABLE paper_trades_new (
                            id INTEGER PRIMARY KEY,
                            signal_id INTEGER,
                            symbol VARCHAR(30) NOT NULL,
                            direction VARCHAR(10) NOT NULL,
                            entry_price FLOAT NOT NULL,
                            entry_date DATE NOT NULL,
                            entry_datetime DATETIME NOT NULL,
                            stop_loss FLOAT NOT NULL,
                            target_price FLOAT NOT NULL,
                            atr_at_entry FLOAT,
                            risk_distance FLOAT,
                            confluence_score INTEGER,
                            alert_tier VARCHAR(30),
                            alert_type VARCHAR(20) NOT NULL DEFAULT 'day_end',
                            rs_rating_at_entry INTEGER,
                            signal_groups_json TEXT,
                            max_favorable_excursion FLOAT,
                            max_adverse_excursion FLOAT,
                            exit_price FLOAT,
                            exit_date DATE,
                            exit_datetime DATETIME,
                            exit_reason VARCHAR(30),
                            pnl_absolute FLOAT,
                            pnl_percent FLOAT,
                            gross_pnl_percent FLOAT,
                            cost_percent FLOAT,
                            r_multiple FLOAT,
                            net_r_multiple FLOAT,
                            fits_tranche_rules BOOLEAN,
                            strategy_version VARCHAR(30),
                            status VARCHAR(10) NOT NULL DEFAULT 'open',
                            created_at DATETIME,
                            updated_at DATETIME,
                            FOREIGN KEY(signal_id) REFERENCES signals (id) ON DELETE SET NULL,
                            CONSTRAINT uq_paper_trade_symbol_dir_date_type UNIQUE (symbol, direction, entry_date, alert_type)
                        )
                    """))
                    existing_cols = [row[1] for row in conn.execute(text("PRAGMA table_info(paper_trades)")).fetchall()]
                    target_cols = [
                        "id", "signal_id", "symbol", "direction", "entry_price", "entry_date", "entry_datetime",
                        "stop_loss", "target_price", "atr_at_entry", "risk_distance", "confluence_score", "alert_tier",
                        "alert_type", "rs_rating_at_entry",
                        "signal_groups_json", "max_favorable_excursion", "max_adverse_excursion", "exit_price",
                        "exit_date", "exit_datetime", "exit_reason", "pnl_absolute", "pnl_percent",
                        "gross_pnl_percent", "cost_percent", "r_multiple", "net_r_multiple", "fits_tranche_rules",
                        "strategy_version", "status", "created_at", "updated_at",
                    ]
                    insert_cols = []
                    select_exprs = []
                    for col in target_cols:
                        if col in existing_cols:
                            insert_cols.append(col)
                            select_exprs.append(col)
                        elif col == "alert_type":
                            insert_cols.append("alert_type")
                            if "alert_tier" in existing_cols:
                                select_exprs.append("CASE WHEN alert_tier LIKE '%intraday%' THEN 'intraday' ELSE 'day_end' END")
                            else:
                                select_exprs.append("'day_end'")
                        elif col == "rs_rating_at_entry":
                            insert_cols.append("rs_rating_at_entry")
                            select_exprs.append("NULL")

                    col_str = ", ".join(insert_cols)
                    expr_str = ", ".join(select_exprs)
                    conn.execute(text(f"INSERT INTO paper_trades_new ({col_str}) SELECT {expr_str} FROM paper_trades"))
                    conn.execute(text("DROP TABLE paper_trades"))
                    conn.execute(text("ALTER TABLE paper_trades_new RENAME TO paper_trades"))
                    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_paper_trades_symbol ON paper_trades (symbol)"))
                    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_paper_trades_signal_id ON paper_trades (signal_id)"))
                    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_paper_trades_entry_date ON paper_trades (entry_date)"))
                    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_paper_trades_status ON paper_trades (status)"))
                    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_paper_trades_alert_type ON paper_trades (alert_type)"))
                    conn.execute(text("COMMIT"))
                except Exception:
                    conn.execute(text("ROLLBACK"))
                    raise
                finally:
                    conn.execute(text("PRAGMA foreign_keys=ON"))
                logger.info("Migrated paper_trades successfully.")


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
        ensure_schema_migrations(_engine)
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
                existing.indicator_detail = json.dumps(old_detail, separators=(",", ":"), default=str)
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
