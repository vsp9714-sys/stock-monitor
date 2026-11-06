"""
Scanner — orchestrates signal detection across all NSE/BSE stocks.

Flow per scan cycle:
1. Get list of all active stocks from DB (refreshed daily from Upstox instruments)
2. For each stock (batched):
   a. Fetch daily OHLCV history (250 days) from Upstox
   b. Run all enabled signal detectors to perform multi-factor checks:
      - RSI (Oversold / Overbought)
      - MACD Crossover
      - EMA Crossovers (9×21, 21×50, etc.)
      - Volume Breakout
   c. Evaluate Confluence:
      - Require consensus direction (no conflicting BUY vs SELL)
      - Require at least N signals to agree (confluence threshold)
   d. For any stock meeting confluence:
      - Fetch fundamental data (from cache or Screener.in)
      - Generate multi-panel chart
      - Save single consolidated signal to DB
      - Send ONE rich alert with all check statuses to Telegram/Discord/Email
3. Deduplication: one alert per stock per direction per trading day
"""
import json
import logging
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd
import numpy as np
from sqlalchemy import func

from src.config_loader import get_config
from src.db import (
    DailyCandleRefresh,
    MarketContextCache,
    OHLCVCache,
    Signal,
    Stock,
    get_delivery_score,
    get_session,
    mark_delivery_sent,
    refresh_signal_notified_state,
    seed_legacy_deliveries,
    upsert_signal,
    upsert_ohlcv_rows,
)
from src.fetcher.fundamentals import FundamentalsFetcher
from src.fetcher.upstox import UpstoxAPIError, UpstoxFetcher
from src.notifiers.base import AlertPayload, BaseNotifier
from src.signals.ema_cross import detect_ema_crossover
from src.signals.macd import detect_macd_signal
from src.signals.rsi import detect_rsi_signal
from src.signals.confluence import evaluate_confluence
from src.signals.volume_breakout import detect_volume_breakout
from src.universe import UniverseSelectionError, UniverseSelector
from src.market_calendar import CalendarUnavailable, IST, is_market_open, is_trading_day
from src.market_context import average_traded_value, regime_for_candles, relative_strength
from src.derivatives import nearest_expiry, summarize_option_chain

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True)
class ScanSummary:
    universe_size: int
    evaluated_count: int
    failed_count: int
    alerts_dispatched: int
    complete: bool
    universe_metadata: dict = field(default_factory=dict)


def _validate_quote(quote: dict, now: datetime, max_age: int) -> tuple[datetime, str | None]:
    """Validate quote price/OHLC consistency and freshness; return trade time or reason."""
    try:
        values = {name: float(quote[name]) for name in (
            "open", "high", "low", "close", "volume", "last_price", "prev_close_price"
        )}
        if not all(math.isfinite(value) for value in values.values()):
            return now, "non_finite_quote_value"
        if any(values[name] <= 0 for name in ("open", "high", "low", "close", "last_price", "prev_close_price")):
            return now, "non_positive_quote_price"
        if values["volume"] < 0:
            return now, "negative_quote_volume"
        low, high = values["low"], values["high"]
        if not (low <= values["open"] <= high and low <= values["close"] <= high):
            return now, "inconsistent_quote_ohlc"
        if not low - 1e-6 <= values["last_price"] <= high + 1e-6:
            return now, "ltp_outside_session_range"

        raw_trade_time = int(quote["last_trade_time"])
        trade_time = datetime.fromtimestamp(raw_trade_time / 1000, timezone.utc)
        age = (now - trade_time).total_seconds()
        if age < -30 or age > max_age:
            return trade_time, "stale_or_future_ltp"

        raw_ohlc_time = int(quote["ts"])
        ohlc_epoch = raw_ohlc_time / 1000 if raw_ohlc_time > 100_000_000_000 else raw_ohlc_time
        ohlc_time = datetime.fromtimestamp(ohlc_epoch, timezone.utc)
        if abs((trade_time - ohlc_time).total_seconds()) > 24 * 60 * 60:
            return trade_time, "invalid_session_ohlc_timestamp"
        return trade_time, None
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return now, "missing_or_invalid_quote_fields"


def _valid_ohlcv_frame(frame: pd.DataFrame) -> bool:
    """Check cached daily rows before they reach a signal detector."""
    required = {"ts", "open", "high", "low", "close", "volume"}
    if frame is None or frame.empty or not required.issubset(frame.columns):
        return False
    try:
        values = frame[["open", "high", "low", "close", "volume"]].astype(float)
        if not np.isfinite(values.to_numpy(dtype=float)).all():
            return False
        if (values[["open", "high", "low", "close"]] <= 0).any().any() or (values["volume"] < 0).any():
            return False
        if (values["low"] > values["high"]).any():
            return False
        if ((values["open"] < values["low"]) | (values["open"] > values["high"])
                | (values["close"] < values["low"]) | (values["close"] > values["high"])).any():
            return False
        timestamps = pd.to_datetime(frame["ts"], errors="coerce")
        return not timestamps.isna().any() and not timestamps.duplicated().any()
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def _daily_timestamp(value) -> datetime:
    """Normalize candle times to a naive IST midnight for daily cache keys."""
    ts = pd.Timestamp(value)
    if ts.tzinfo is not None:
        ts = ts.tz_convert(IST)
    return datetime.combine(ts.date(), datetime_time.min)


def _build_notifiers(cfg: dict) -> list[BaseNotifier]:
    """Build enabled notifiers from config."""
    notifiers = []
    notif_cfg = cfg.get("notifications", {})

    if notif_cfg.get("telegram", {}).get("enabled"):
        try:
            from src.notifiers.telegram import TelegramNotifier
            notifiers.append(TelegramNotifier(cfg))
            logger.info("Telegram notifier enabled.")
        except Exception as e:
            logger.error(f"Failed to init Telegram notifier: {e}")

    if notif_cfg.get("discord", {}).get("enabled"):
        try:
            from src.notifiers.discord import DiscordNotifier
            notifiers.append(DiscordNotifier(cfg))
            logger.info("Discord notifier enabled.")
        except Exception as e:
            logger.error(f"Failed to init Discord notifier: {e}")

    if notif_cfg.get("email", {}).get("enabled"):
        try:
            from src.notifiers.email_notifier import EmailNotifier
            notifiers.append(EmailNotifier(cfg))
            logger.info("Email notifier enabled.")
        except Exception as e:
            logger.error(f"Failed to init Email notifier: {e}")

    if not notifiers:
        logger.warning("No notifiers enabled! Configure at least one in config.yaml.")
    return notifiers


class Scanner:
    """Main scanning engine with confluence filtering."""

    def __init__(self):
        self.cfg = get_config()
        self.fetcher = UpstoxFetcher()
        self.fundamentals = FundamentalsFetcher()
        self.notifiers = _build_notifiers(self.cfg)
        self.signal_cfg = self.cfg.get("signals", {})
        self.universe_cfg = self.cfg.get("universe", {})
        self.confluence_cfg = self.cfg.get("confluence", {})
        from src.config_loader import validate_signal_config
        validate_signal_config(self.cfg)
        self.universe_selector = UniverseSelector(self.cfg, get_session)
        self._last_universe_metadata = {}
        self._oi_cache = {}
        self._oi_fno_symbols = None
        self._oi_fno_date = None
        self._oi_fetch_count = 0

    def _get_market_index_frame(self, index_key: str, as_of_date: date) -> pd.DataFrame | None:
        """Load a fresh persisted index cache, refreshing through Upstox when stale."""
        context_cfg = self.cfg.get("market_context", {})
        ttl = timedelta(hours=float(context_cfg.get("cache_ttl_hours", 12)))
        db = get_session()
        try:
            cached = db.query(MarketContextCache).filter_by(context_key=index_key).first()
            if cached and cached.fetched_at:
                fetched_at = cached.fetched_at
                if fetched_at.tzinfo is None:
                    fetched_at = fetched_at.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) - fetched_at < ttl:
                    try:
                        rows = json.loads(cached.candles_json)
                        frame = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
                        dates = pd.to_datetime(frame["ts"], errors="coerce").map(
                            lambda value: _daily_timestamp(value).date() if not pd.isna(value) else None
                        )
                        frame = frame.loc[dates.notna() & (dates <= as_of_date)].copy()
                        if not frame.empty and _valid_ohlcv_frame(frame):
                            return frame.sort_values("ts").reset_index(drop=True)
                    except (ValueError, TypeError, json.JSONDecodeError):
                        logger.warning("Discarding malformed market-context cache for %s", index_key)
        finally:
            db.close()

        days = max(120, int(context_cfg.get("long_sma_sessions", 50)) * 3)
        try:
            from_date = (as_of_date - timedelta(days=days)).isoformat()
            frame = self.fetcher.get_historical_candles(
                index_key, "day", from_date=from_date, to_date=as_of_date.isoformat()
            )
            if frame is None or frame.empty:
                return None
            frame = frame[["ts", "open", "high", "low", "close", "volume"]].copy()
            frame["ts"] = pd.to_datetime(frame["ts"], errors="coerce")
            frame = frame.loc[frame["ts"].notna()].copy()
            frame = frame.loc[frame["ts"].map(lambda value: _daily_timestamp(value).date()) <= as_of_date]
            frame = frame.sort_values("ts").drop_duplicates(subset=["ts"], keep="last").reset_index(drop=True)
            if frame.empty:
                return None
            if not _valid_ohlcv_frame(frame):
                logger.warning("Ignoring invalid market-context candles for %s", index_key)
                return None
            rows = []
            for row in frame.itertuples(index=False, name=None):
                ts = pd.Timestamp(row[0])
                rows.append([ts.isoformat(), *[float(value) for value in row[1:]]])
            db = get_session()
            try:
                cached = db.query(MarketContextCache).filter_by(context_key=index_key).first()
                if cached is None:
                    cached = MarketContextCache(context_key=index_key, candles_json="[]", fetched_at=datetime.now(timezone.utc))
                    db.add(cached)
                cached.candles_json = json.dumps(rows)
                cached.fetched_at = datetime.now(timezone.utc)
                db.commit()
            finally:
                db.close()
            return frame.reset_index(drop=True)
        except Exception as exc:
            logger.info("Market context unavailable for %s: %s", index_key, exc)
            return None

    def _market_context_for(self, stock_frame: pd.DataFrame, fund: dict | None) -> dict:
        cfg = self.cfg.get("market_context", {})
        if not cfg.get("enabled", True):
            return {"status": "disabled"}
        benchmark_key = cfg.get("benchmark_key", "NSE_INDEX|Nifty 50")
        as_of_date = pd.to_datetime(stock_frame["ts"].iloc[-1]).date() if stock_frame is not None and not stock_frame.empty else date.min
        benchmark = self._get_market_index_frame(benchmark_key, as_of_date)
        short = int(cfg.get("short_sma_sessions", 20))
        long = int(cfg.get("long_sma_sessions", 50))
        lookback = int(cfg.get("lookback_sessions", 20))
        benchmark_regime = regime_for_candles(benchmark, short_window=short, long_window=long)
        benchmark_strength = relative_strength(stock_frame, benchmark, lookback=lookback)
        sector = (fund or {}).get("sector")
        sector_key = (cfg.get("sector_index_keys", {}) or {}).get(sector)
        sector_frame = self._get_market_index_frame(sector_key, as_of_date) if sector_key else None
        sector_regime = (
            regime_for_candles(sector_frame, short_window=short, long_window=long)
            if sector_key else {"status": "unavailable", "reason": "no_configured_sector_index", "sector": sector}
        )
        sector_strength = relative_strength(stock_frame, sector_frame, lookback=lookback) if sector_key else {
            "status": "unavailable", "reason": "no_configured_sector_index"
        }
        return {
            "status": "available" if benchmark_regime.get("status") == "available" else "partial",
            "mode": cfg.get("mode", "informational"),
            "benchmark_key": benchmark_key,
            "benchmark": benchmark_regime,
            "benchmark_relative_strength": benchmark_strength,
            "sector": sector,
            "sector_key": sector_key,
            "sector_regime": sector_regime,
            "sector_relative_strength": sector_strength,
        }

    def _derivatives_context_for(self, stock: Stock, session_date: date) -> dict:
        cfg = self.cfg.get("derivatives", {}) or {}
        if not cfg.get("enabled", False):
            return {"status": "disabled", "source": "Upstox option-chain API"}
        ttl = timedelta(minutes=float(cfg.get("cache_ttl_minutes", 15)))
        cached = self._oi_cache.get(stock.symbol)
        if cached and datetime.now(timezone.utc) - cached[0] < ttl:
            return cached[1]
        limit = int(cfg.get("max_context_symbols_per_scan", 25))
        if self._oi_fetch_count >= limit:
            return {"status": "unavailable", "reason": "configured_scan_limit"}
        if self._oi_fno_date != session_date or self._oi_fno_symbols is None:
            self._oi_fno_symbols, meta = self.universe_selector.get_fno_underlyings(session_date)
            self._oi_fno_date = session_date
            if not meta.get("available", True):
                return {"status": "unavailable", "reason": "fno_eligibility_source_unavailable"}
        if stock.symbol not in self._oi_fno_symbols:
            return {"status": "unavailable", "reason": "not_fno_eligible"}
        try:
            self._oi_fetch_count += 1
            contracts = self.fetcher.get_option_contracts(stock.instrument_key)
            expiry = nearest_expiry(contracts, session_date)
            if expiry is None:
                result = {"status": "unavailable", "reason": "no_current_option_expiry"}
            else:
                rows = self.fetcher.get_option_chain(stock.instrument_key, expiry)
                result = summarize_option_chain(rows, expiry_date=expiry)
        except Exception as exc:
            logger.info("Optional OI context unavailable for %s: %s", stock.symbol, exc)
            result = {"status": "unavailable", "reason": "provider_error"}
        self._oi_cache[stock.symbol] = (datetime.now(timezone.utc), result)
        return result

    # ─── Stock Universe ──────────────────────────────────────────────────

    def refresh_instrument_master(self) -> int:
        """Refresh NSE ordinary shares, deactivating bonds and stale listings."""
        logger.info("Refreshing NSE common-equity instrument master...")
        session = get_session()
        try:
            count = 0
            for exchange in ["NSE"]:
                instruments = self.fetcher.get_all_eq_symbols(exchange)
                active_symbols = {inst["symbol"] for inst in instruments}
                if not active_symbols:
                    raise RuntimeError("Refusing to deactivate the NSE universe: refreshed list is empty")
                for inst in instruments:
                    existing = session.query(Stock).filter_by(
                        symbol=inst["symbol"], exchange=exchange
                    ).first()
                    now = datetime.now(timezone.utc)
                    if existing:
                        existing.instrument_key = inst["instrument_key"]
                        existing.isin = inst.get("isin", "")
                        existing.company_name = inst.get("company_name", "")
                        existing.series = inst.get("series", existing.series)
                        existing.is_active = True
                        existing.last_updated = now
                    else:
                        session.add(Stock(
                            symbol=inst["symbol"],
                            isin=inst.get("isin", ""),
                            company_name=inst.get("company_name", ""),
                            exchange=exchange,
                            instrument_key=inst["instrument_key"],
                            series=inst.get("series", "EQ"),
                            is_active=True,
                            last_updated=now,
                        ))
                    count += 1
                    if count % 500 == 0:
                        session.commit()
                # Upstox's NSE_EQ/EQUITY classification includes bonds; anything
                # absent from the verified NSE common-equity cross-check is inactive.
                session.query(Stock).filter(
                    Stock.exchange == exchange,
                    Stock.is_active.is_(True),
                    ~Stock.symbol.in_(active_symbols),
                ).update({Stock.is_active: False}, synchronize_session=False)
            session.commit()
            logger.info(f"Instrument master refreshed: {count} common stocks; stale/debt rows deactivated.")
            return count
        finally:
            session.close()

    def get_active_stocks(self, limit: Optional[int] = None) -> list[Stock]:
        """Resolve the configured universe against the verified instrument master."""
        session = get_session()
        try:
            candidates = session.query(Stock).filter_by(is_active=True, exchange="NSE").order_by(Stock.symbol).all()
            session.expunge_all()
        finally:
            session.close()
        result = self.universe_selector.resolve(candidates, now=datetime.now(IST))
        self._last_universe_metadata = result.metadata
        stocks = result.symbols
        return stocks[:limit] if limit else stocks

    def required_daily_candles(self) -> int:
        pairs = self.signal_cfg.get("ema_crossover", {}).get("pairs", [[9, 21], [21, 50], [50, 200]])
        slowest = max((int(pair[1]) for pair in pairs), default=50)
        return max(slowest + 10, int(self.signal_cfg.get("rsi", {}).get("period", 14)) + 10, 30)

    def warm_candle_cache(self, limit: Optional[int] = None, checkpoint_symbols: int = 20) -> dict:
        """Idempotently backfill missing daily history; DB rows are resume checkpoints."""
        stocks = self.get_active_stocks(limit=limit)
        minimum = self.required_daily_candles()
        session = get_session()
        pending_rows = []
        pending_symbols = 0
        warmed = 0
        skipped = 0
        failures = []
        insufficient_history = []
        stopped_early = False
        try:
            counts = dict(
                session.query(OHLCVCache.symbol, func.count(OHLCVCache.id))
                .filter(OHLCVCache.interval == "1d")
                .group_by(OHLCVCache.symbol)
                .all()
            )
            for index, stock in enumerate(stocks, 1):
                if counts.get(stock.symbol, 0) >= minimum:
                    skipped += 1
                    continue
                try:
                    candles = self.fetcher.get_daily_candles(stock.instrument_key, days=365)
                    if candles.empty:
                        failures.append(stock.symbol)
                        logger.warning(
                            "Cache warm-up received no daily bars for %s", stock.symbol,
                        )
                        continue
                    current_ist = datetime.now(IST)
                    before_daily_finalization = current_ist.time() < datetime_time(15, 45)
                    symbol_rows = []
                    for row in candles.itertuples(index=False):
                        candle_ts = _daily_timestamp(row.ts)
                        if before_daily_finalization and candle_ts.date() >= current_ist.date():
                            continue
                        symbol_rows.append({
                            "symbol": stock.symbol,
                            "interval": "1d",
                            "ts": candle_ts,
                            "open": float(row.open),
                            "high": float(row.high),
                            "low": float(row.low),
                            "close": float(row.close),
                            "volume": float(row.volume),
                        })
                    if not symbol_rows:
                        insufficient_history.append(stock.symbol)
                        logger.info("No finalized daily candles available yet for %s", stock.symbol)
                        continue
                    pending_rows.extend(symbol_rows)
                    pending_symbols += 1
                    warmed += 1
                    counts[stock.symbol] = len(symbol_rows)
                    if len(symbol_rows) < minimum:
                        insufficient_history.append(stock.symbol)
                        logger.info(
                            "Cached %s bars for %s; %s are needed for every configured check",
                            len(symbol_rows), stock.symbol, minimum,
                        )
                    if pending_symbols >= checkpoint_symbols:
                        upsert_ohlcv_rows(session, pending_rows)
                        pending_rows.clear()
                        pending_symbols = 0
                    if index % 100 == 0 or index == len(stocks):
                        logger.info(
                            "Daily cache warm-up progress: %s/%s visited; %s warmed, %s already complete, %s failed",
                            index, len(stocks), warmed, skipped, len(failures),
                        )
                except Exception as exc:
                    failures.append(stock.symbol)
                    logger.warning("Cache warm-up failed for %s: %s", stock.symbol, exc)
                    if isinstance(exc, UpstoxAPIError):
                        # A provider-level failure (notably exhausted 429 retries
                        # or an invalid token) should pause the job. Checkpointed
                        # rows remain committed and the next invocation resumes.
                        stopped_early = True
                        break
            if pending_rows:
                upsert_ohlcv_rows(session, pending_rows)
            session.commit()
            result = {
                "universe_size": len(stocks),
                "warmed": warmed,
                "already_complete": skipped,
                "failed": len(failures),
                "failed_symbols": failures,
                "insufficient_history": len(insufficient_history),
                "insufficient_history_symbols": insufficient_history,
                "stopped_early": stopped_early,
                "remaining": max(0, len(stocks) - warmed - skipped - len(failures)),
                "minimum_daily_candles": minimum,
            }
            logger.info("Daily cache warm-up finished: %s", result)
            return result
        finally:
            session.close()

    def refresh_completed_daily_cache(
        self,
        limit: Optional[int] = None,
        checkpoint_symbols: int = 20,
        *,
        now: Optional[datetime] = None,
        target_session: Optional[date] = None,
    ) -> dict:
        """Refresh canonical daily candles for a provider-confirmed session.

        The service's intraday quote sweep deliberately does not mutate this
        cache. This bounded, paced job fetches provider historical candles for
        an explicit or current session. Same-day cache rows are not proof of finalization: an older
        warm-up may have stored them before close. Per-symbol refresh state
        makes verified rows resumable while unverified same-day rows are removed
        before requests so a restart cannot treat them as confirmed data.
        """
        current = now or datetime.now(IST)
        if current.tzinfo is None:
            current = current.replace(tzinfo=IST)
        current = current.astimezone(IST)
        if current.weekday() >= 5:
            logger.info("Daily candle refresh skipped on weekend.")
            return {"universe_size": 0, "updated": 0, "failed": 0, "skipped": True}
        if target_session is None and current.time() < datetime_time(15, 45):
            logger.info("Daily candle refresh skipped before post-close finalization window.")
            return {"universe_size": 0, "updated": 0, "failed": 0, "skipped": True}
        target_day = target_session or current.date()
        if target_day > current.date():
            raise ValueError("Daily refresh target session cannot be in the future")

        stocks = self.get_active_stocks(limit=limit)
        session = get_session()
        updated = 0
        unchanged = 0
        processed = 0
        failed = []
        pending_rows = []
        pending_symbols = 0
        stopped_early = False
        try:
            refresh_state = {
                state.symbol: state
                for state in session.query(DailyCandleRefresh).all()
            }
            current_midnight = datetime.combine(target_day, datetime_time.min)
            # Existing same-session rows have unknown provenance. Remove them
            # unless this day's post-close fetch already confirmed that symbol.
            unverified_symbols = [
                stock.symbol for stock in stocks
                if not (
                    (state := refresh_state.get(stock.symbol))
                    and state.session_date == target_day
                    and state.status == "confirmed"
                )
            ]
            for offset in range(0, len(unverified_symbols), 400):
                batch = unverified_symbols[offset:offset + 400]
                session.query(OHLCVCache).filter(
                    OHLCVCache.interval == "1d",
                    OHLCVCache.symbol.in_(batch),
                    OHLCVCache.ts == current_midnight,
                ).delete(synchronize_session=False)
            # Persist the conservative cleanup before network work. If this
            # process stops, scans fall back to prior confirmed sessions.
            session.commit()
            latest_by_symbol = dict(
                session.query(OHLCVCache.symbol, func.max(OHLCVCache.ts))
                .filter(OHLCVCache.interval == "1d")
                .group_by(OHLCVCache.symbol)
                .all()
            )
            for index, stock in enumerate(stocks, 1):
                state = refresh_state.get(stock.symbol)
                if state and state.session_date == target_day and state.status == "confirmed":
                    unchanged += 1
                    continue
                last_cached = latest_by_symbol.get(stock.symbol)
                if last_cached is not None:
                    last_date = pd.Timestamp(last_cached).date()
                    from_date = last_date.isoformat()
                else:
                    from_date = (target_day - timedelta(days=365)).isoformat()
                processed += 1
                try:
                    candles = self.fetcher.get_historical_candles(
                        stock.instrument_key,
                        "day",
                        from_date=from_date,
                        to_date=target_day.isoformat(),
                    )
                    if candles.empty:
                        failed.append(stock.symbol)
                        refresh_state[stock.symbol] = DailyCandleRefresh(
                            symbol=stock.symbol, session_date=target_day,
                            status="no_bar", updated_at=datetime.now(timezone.utc),
                        )
                        session.merge(refresh_state[stock.symbol])
                        logger.warning("Daily refresh received no bars for %s", stock.symbol)
                        pending_symbols += 1
                        if pending_symbols >= checkpoint_symbols:
                            if pending_rows:
                                upsert_ohlcv_rows(session, pending_rows, commit=False)
                                pending_rows.clear()
                            session.commit()
                            pending_symbols = 0
                        continue
                    symbol_rows = []
                    for row in candles.itertuples(index=False):
                        ts = _daily_timestamp(row.ts)
                        # Never persist today's unfinished bar. The refresh runs
                        # after close; if the provider has not published today's
                        # completed candle yet, a later run will fetch it.
                        if ts.date() > target_day:
                            continue
                        symbol_rows.append({
                            "symbol": stock.symbol,
                            "interval": "1d",
                            "ts": ts,
                            "open": float(row.open),
                            "high": float(row.high),
                            "low": float(row.low),
                            "close": float(row.close),
                            "volume": float(row.volume),
                        })
                    has_current_bar = any(row["ts"].date() == target_day for row in symbol_rows)
                    pending_rows.extend(symbol_rows)
                    pending_symbols += 1
                    if has_current_bar:
                        updated += 1
                        refresh_state[stock.symbol] = DailyCandleRefresh(
                            symbol=stock.symbol, session_date=target_day,
                            status="confirmed", updated_at=datetime.now(timezone.utc),
                        )
                    else:
                        # A successful response without today's completed bar
                        # is not a valid confirmation. The unverified same-day
                        # row was removed above and must stay absent.
                        failed.append(stock.symbol)
                        refresh_state[stock.symbol] = DailyCandleRefresh(
                            symbol=stock.symbol, session_date=target_day,
                            status="no_bar", updated_at=datetime.now(timezone.utc),
                        )
                    session.merge(refresh_state[stock.symbol])
                    latest_by_symbol[stock.symbol] = max(
                        (row["ts"] for row in symbol_rows),
                        default=last_cached,
                    )
                    if pending_symbols >= checkpoint_symbols:
                        upsert_ohlcv_rows(session, pending_rows, commit=False)
                        pending_rows.clear()
                        session.commit()
                        pending_symbols = 0
                    if index % 100 == 0 or index == len(stocks):
                        logger.info(
                            "Post-close daily refresh progress: %s/%s visited; %s updated; %s failed",
                            index, len(stocks), updated, len(failed),
                        )
                except Exception as exc:
                    failed.append(stock.symbol)
                    refresh_state[stock.symbol] = DailyCandleRefresh(
                        symbol=stock.symbol, session_date=target_day,
                        status="failed", updated_at=datetime.now(timezone.utc),
                    )
                    session.merge(refresh_state[stock.symbol])
                    logger.warning("Daily refresh failed for %s: %s", stock.symbol, exc)
                    if isinstance(exc, UpstoxAPIError) and exc.error_code == "UDAPI100011":
                        # A bad instrument mapping is isolated to this symbol;
                        # preserve its failed checkpoint but continue refreshing
                        # the rest of the active universe.
                        pending_symbols += 1
                        if pending_symbols >= checkpoint_symbols:
                            if pending_rows:
                                upsert_ohlcv_rows(session, pending_rows, commit=False)
                                pending_rows.clear()
                            session.commit()
                            pending_symbols = 0
                        continue
                    if isinstance(exc, UpstoxAPIError):
                        # Provider-wide/transient failures fail closed so the
                        # run can resume after service/provider recovery.
                        stopped_early = True
                        break
            if pending_rows:
                upsert_ohlcv_rows(session, pending_rows, commit=False)
            session.commit()
            result = {
                "universe_size": len(stocks),
                "processed": processed,
                "updated": updated,
                "unchanged": unchanged,
                "failed": len(failed),
                "failed_symbols": failed,
                "stopped_early": stopped_early,
                # Failed symbols are included in processed because each was
                # attempted; unchanged symbols are confirmed checkpoints.
                "remaining": max(0, len(stocks) - processed - unchanged),
                "session_date": target_day.isoformat(),
            }
            logger.info("Post-close daily candle refresh finished: %s", result)
            return result
        finally:
            session.close()

    def refresh_latest_completed_daily_cache(
        self,
        *,
        now: Optional[datetime] = None,
        checkpoint_symbols: int = 20,
        manual: bool = False,
    ) -> dict:
        """Probe Upstox at the scheduled 06:00 IST run, then refresh the latest completed session.

        A small liquid-symbol readiness probe avoids a full-universe pass while
        the provider is still publishing the prior session. The full pass is
        started only when all probe symbols expose the same latest candle date.
        """
        current = now or datetime.now(IST)
        if current.tzinfo is None:
            current = current.replace(tzinfo=IST)
        current = current.astimezone(IST)
        try:
            trading_day = is_trading_day(current.date(), getattr(self, "cfg", {}).get("market", {}))
        except CalendarUnavailable as exc:
            logger.error("Morning refresh skipped: %s", exc)
            return {"universe_size": 0, "updated": 0, "failed": 0, "skipped": True,
                    "reason": "calendar_unavailable"}
        if (not manual and not trading_day) or (
            not manual and not (datetime_time(6, 0) <= current.time() < datetime_time(7, 0))
        ):
            logger.info("Morning daily-candle readiness check skipped outside its weekday schedule window.")
            return {"universe_size": 0, "updated": 0, "failed": 0, "skipped": True}

        stocks = {stock.symbol: stock for stock in self.get_active_stocks()}
        probe_symbols = ("RELIANCE", "TCS", "360ONE")
        missing = [symbol for symbol in probe_symbols if symbol not in stocks]
        if missing:
            logger.warning("Daily refresh readiness probe missing active symbols: %s", missing)
            return {"universe_size": len(stocks), "updated": 0, "failed": 0, "skipped": True,
                    "reason": "probe_symbols_missing"}

        probe_dates = []
        probe_from = (current.date() - timedelta(days=7)).isoformat()
        for symbol in probe_symbols:
            stock = stocks[symbol]
            candles = self.fetcher.get_historical_candles(
                stock.instrument_key, "day", from_date=probe_from, to_date=current.date().isoformat()
            )
            if candles.empty:
                logger.info("Daily refresh not ready: provider returned no recent candles for %s", symbol)
                return {"universe_size": len(stocks), "updated": 0, "failed": 0, "skipped": True,
                        "reason": "provider_not_ready", "probe_symbol": symbol}
            latest = max(_daily_timestamp(row.ts).date() for row in candles.itertuples(index=False))
            probe_dates.append(latest)

        if len(set(probe_dates)) != 1:
            logger.info("Daily refresh not ready: probe symbols disagree on latest session dates: %s", probe_dates)
            return {"universe_size": len(stocks), "updated": 0, "failed": 0, "skipped": True,
                    "reason": "probe_dates_disagree", "probe_dates": [d.isoformat() for d in probe_dates]}

        target = probe_dates[0]
        session = get_session()
        try:
            cached_latest = session.query(func.max(OHLCVCache.ts)).filter(OHLCVCache.interval == "1d").scalar()
            if cached_latest is not None and target < pd.Timestamp(cached_latest).date():
                logger.warning("Daily refresh probe date %s is behind cache date %s; refusing regression.",
                               target, pd.Timestamp(cached_latest).date())
                return {"universe_size": len(stocks), "updated": 0, "failed": 0, "skipped": True,
                        "reason": "provider_behind_cache", "session_date": target.isoformat()}
        finally:
            session.close()

        logger.info("Daily refresh readiness confirmed for session %s; starting paced full-universe refresh.", target)
        return self.refresh_completed_daily_cache(
            now=current, checkpoint_symbols=checkpoint_symbols, target_session=target
        )

    # ─── Single Stock Scan & Confluence Evaluation ────────────────────────

    def _scan_stock(
        self, stock: Stock, df: pd.DataFrame, provisional: bool = False
    ) -> tuple[Optional[dict], pd.DataFrame]:
        """
        Scan a single stock across all indicator checks and evaluate confluence.
        Returns (confluence_dict or None, df of daily candles).
        """
        try:
            if df.empty or len(df) < 30:
                return None, None

            price_floor = self.universe_cfg.get("min_price", 10)
            current_price = float(df["close"].iloc[-1])
            if current_price < price_floor:
                return None, None  # Skip penny stocks

            checks = []

            # ── 1. RSI Check ────────────────────────────────────────────
            if self.signal_cfg.get("rsi", {}).get("enabled", True):
                rsi_cfg = self.signal_cfg["rsi"]
                oversold = rsi_cfg.get("oversold_threshold", 30)
                overbought = rsi_cfg.get("overbought_threshold", 70)
                rsi_result = detect_rsi_signal(
                    stock.symbol, df,
                    period=rsi_cfg.get("period", 14),
                    oversold=oversold,
                    overbought=overbought,
                    mode=rsi_cfg.get("mode", "mean_reversion"),
                    confirmation=rsi_cfg.get("confirmation", "level"),
                    bullish_threshold=rsi_cfg.get("bullish_threshold", 55),
                    bearish_threshold=rsi_cfg.get("bearish_threshold", 45),
                )
                rsi_val = rsi_result.rsi_value
                checks.append({
                    "name": f"RSI ({rsi_cfg.get('period', 14)})",
                    "signal_type": "rsi",
                    "event_met": rsi_result.event_triggered,
                    "event_direction": rsi_result.event_direction,
                    "state_met": rsi_result.trend_direction is not None,
                    "state_direction": rsi_result.trend_direction,
                    "met": rsi_result.triggered,
                    "direction": rsi_result.direction,
                    "value_text": f"RSI {rsi_val:.1f}",
                    "description": rsi_result.trigger_reason or "No selected RSI trigger",
                    "rsi_mode": rsi_result.mode,
                })

            # ── 2. MACD Check ───────────────────────────────────────────
            if self.signal_cfg.get("macd", {}).get("enabled", True):
                macd_cfg = self.signal_cfg["macd"]
                macd_result = detect_macd_signal(
                    stock.symbol, df,
                    fast=macd_cfg.get("fast_period", 12),
                    slow=macd_cfg.get("slow_period", 26),
                    signal_period=macd_cfg.get("signal_period", 9),
                )
                if macd_result.triggered:
                    checks.append({
                        "name": "MACD (12,26,9)",
                        "signal_type": "macd",
                        "event_met": macd_result.triggered,
                        "event_direction": macd_result.direction,
                        "state_met": pd.notna(macd_result.macd_value) and pd.notna(macd_result.signal_value)
                                    and macd_result.macd_value != macd_result.signal_value,
                        "state_direction": (
                            "BUY" if pd.notna(macd_result.macd_value) and pd.notna(macd_result.signal_value)
                            and macd_result.macd_value > macd_result.signal_value else
                            "SELL" if pd.notna(macd_result.macd_value) and pd.notna(macd_result.signal_value)
                            and macd_result.macd_value < macd_result.signal_value else None
                        ),
                        "met": True,
                        "direction": macd_result.direction,
                        "value_text": f"MACD {macd_result.macd_value:.2f}",
                        "description": "Bullish Crossover" if macd_result.direction == "BUY" else "Bearish Crossover",
                    })
                else:
                    checks.append({
                        "name": "MACD (12,26,9)",
                        "signal_type": "macd",
                        "event_met": False,
                        "event_direction": None,
                        "state_met": pd.notna(macd_result.macd_value) and pd.notna(macd_result.signal_value)
                                    and macd_result.macd_value != macd_result.signal_value,
                        "state_direction": (
                            "BUY" if pd.notna(macd_result.macd_value) and pd.notna(macd_result.signal_value)
                            and macd_result.macd_value > macd_result.signal_value else
                            "SELL" if pd.notna(macd_result.macd_value) and pd.notna(macd_result.signal_value)
                            and macd_result.macd_value < macd_result.signal_value else None
                        ),
                        "met": False,
                        "direction": None,
                        "value_text": f"Hist {macd_result.histogram:+.2f}",
                        "description": "No Cross",
                    })

            # ── 3. EMA Crossover Checks ─────────────────────────────────
            if self.signal_cfg.get("ema_crossover", {}).get("enabled", True):
                ema_cfg = self.signal_cfg["ema_crossover"]
                pairs = [tuple(p) for p in ema_cfg.get("pairs", [[9, 21], [21, 50]])]
                for fast_p, slow_p in pairs:
                    chk_name = f"EMA {fast_p}×{slow_p}"
                    ema_res = detect_ema_crossover(stock.symbol, df, fast_p, slow_p)
                    if pd.isna(ema_res.fast_ema_value) or pd.isna(ema_res.slow_ema_value):
                        checks.append({
                            "name": chk_name,
                            "signal_type": f"ema_{fast_p}x{slow_p}",
                            "event_met": False,
                            "event_direction": None,
                            "state_met": False,
                            "state_direction": None,
                            "met": False,
                            "direction": None,
                            "value_text": "N/A",
                            "description": "Insufficient Data",
                        })
                        continue

                    rel = f"{fast_p} above {slow_p}" if ema_res.fast_ema_value > ema_res.slow_ema_value else f"{fast_p} below {slow_p}"
                    if ema_res.triggered:
                        checks.append({
                            "name": chk_name,
                            "signal_type": f"ema_{fast_p}x{slow_p}",
                            "event_met": True,
                            "event_direction": ema_res.direction,
                            "state_met": ema_res.fast_ema_value != ema_res.slow_ema_value,
                            "state_direction": "BUY" if ema_res.fast_ema_value > ema_res.slow_ema_value else "SELL",
                            "met": True,
                            "direction": ema_res.direction,
                            "value_text": f"₹{ema_res.fast_ema_value:.1f} vs ₹{ema_res.slow_ema_value:.1f}",
                            "description": f"{ema_res.direction.title()} Cross",
                        })
                    else:
                        checks.append({
                            "name": chk_name,
                            "signal_type": f"ema_{fast_p}x{slow_p}",
                            "event_met": False,
                            "event_direction": None,
                            "state_met": ema_res.fast_ema_value != ema_res.slow_ema_value,
                            "state_direction": "BUY" if ema_res.fast_ema_value > ema_res.slow_ema_value else "SELL",
                            "met": False,
                            "direction": None,
                            "value_text": f"₹{ema_res.fast_ema_value:.1f} ({rel})",
                            "description": "No Cross",
                        })

            # ── 4. Volume Breakout Check ────────────────────────────────
            if self.signal_cfg.get("volume_breakout", {}).get("enabled", True):
                vol_cfg = self.signal_cfg["volume_breakout"]
                vol_result = detect_volume_breakout(
                    stock.symbol, df,
                    volume_multiplier=vol_cfg.get("volume_multiplier", 2.0),
                    lookback_days=vol_cfg.get("breakout_lookback_days", 20),
                )
                vol_ratio = vol_result.volume_ratio
                if vol_result.triggered:
                    if vol_result.direction in ("BUY", "SELL"):
                        checks.append({
                            "name": "Volume Breakout",
                            "signal_type": "volume_breakout",
                            "event_met": True,
                            "event_direction": vol_result.direction,
                            "state_met": True,
                            "state_direction": vol_result.direction,
                            "met": True,
                            "direction": vol_result.direction,
                            "value_text": f"{vol_ratio:.1f}× Avg Vol",
                            "description": (
                                ("Bullish Breakout" if vol_result.direction == "BUY" else "Bearish Breakdown")
                                + (" (partial-session volume vs full-day history)" if provisional else "")
                            ),
                        })
                    else:
                        checks.append({
                            "name": "Volume Breakout",
                            "signal_type": "volume_breakout",
                            "event_met": False,
                            "event_direction": None,
                            "state_met": False,
                            "state_direction": None,
                            "met": False,
                            "direction": None,
                            "value_text": f"{vol_ratio:.1f}× Avg Vol",
                            "description": (
                                "Spike (No Price Breakout)"
                                + (" (partial-session volume vs full-day history)" if provisional else "")
                            ),
                        })
                else:
                    checks.append({
                        "name": "Volume Breakout",
                        "signal_type": "volume_breakout",
                        "event_met": False,
                        "event_direction": None,
                        "state_met": False,
                        "state_direction": None,
                        "met": False,
                        "direction": None,
                        "value_text": f"{vol_ratio:.1f}× Avg Vol",
                        "description": (
                            "Normal Volume (partial-session volume vs full-day history)"
                            if provisional else "Normal Volume"
                        ),
                    })

            # ── Evaluate Confluence ─────────────────────────────────────
            confluence = evaluate_confluence(
                checks,
                mode=self.confluence_cfg.get("mode", "crossover"),
                minimum_groups=self.confluence_cfg.get(
                    "min_groups_to_alert", self.confluence_cfg.get("min_signals_to_alert", 2)
                ),
                direction_consensus=self.confluence_cfg.get("direction_consensus", True),
            )
            consensus_dir = confluence["direction"]
            met_count = confluence["score"]
            if consensus_dir is None:
                return None, df

            # Confluence criteria satisfied!
            result = {
                "symbol": stock.symbol,
                "company_name": stock.company_name or stock.symbol,
                "direction": consensus_dir,
                "confluence_score": met_count,
                "total_signals": confluence["total_groups"],
                "group_votes": confluence["group_votes"],
                "conflicting_groups": confluence["conflicting_groups"],
                "confluence_mode": self.confluence_cfg.get("mode", "crossover"),
                "score_version": 2,
                "price": current_price,
                "checks": confluence["checks"],
                "signal_type": (
                    f"provisional_intraday_confluence_{consensus_dir.lower()}"
                    if provisional else f"confluence_{consensus_dir.lower()}"
                ),
                "signal_horizon": "provisional_intraday" if provisional else "confirmed_daily",
            }
            return result, df

        except Exception as e:
            logger.error(f"Error scanning {stock.symbol}: {e}", exc_info=False)
            raise

    # ─── Full Scan Cycle ─────────────────────────────────────────────────

    def _load_cached_daily_frames(self, stocks: list[Stock], session) -> dict[str, pd.DataFrame]:
        symbols = [stock.symbol for stock in stocks]
        grouped = {symbol: [] for symbol in symbols}
        # Keep IN clauses portable to SQLite builds with the older 999-bind limit.
        for offset in range(0, len(symbols), 400):
            batch = symbols[offset:offset + 400]
            rows = (
                session.query(
                    OHLCVCache.symbol, OHLCVCache.ts, OHLCVCache.open,
                    OHLCVCache.high, OHLCVCache.low, OHLCVCache.close,
                    OHLCVCache.volume,
                )
                .filter(OHLCVCache.interval == "1d", OHLCVCache.symbol.in_(batch))
                .order_by(OHLCVCache.symbol, OHLCVCache.ts)
                .yield_per(10000)
            )
            for row in rows:
                grouped[row.symbol].append(tuple(row[1:]))
        return {
            symbol: pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
            for symbol, rows in grouped.items()
        }

    def run_scan(self, limit: Optional[int] = None, *, now: Optional[datetime] = None) -> ScanSummary:
        """Scan cached history plus batched live OHLCV and report coverage gaps."""
        logger.info("Starting market scan with cached history and batched quotes...")
        self._oi_fetch_count = 0
        try:
            stocks = self.get_active_stocks(limit=limit)
        except UniverseSelectionError as exc:
            metadata = {"mode": self.universe_cfg.get("mode", "all_nse"),
                        "selection_complete": False, "selection_error": str(exc)}
            logger.error("Scan incomplete: configured universe could not be selected: %s", exc)
            return ScanSummary(0, 0, 0, 0, False, metadata)
        universe_metadata = dict(getattr(self, "_last_universe_metadata", {}))
        if not stocks:
            logger.error("Scan incomplete: configured universe resolved to no stocks.")
            return ScanSummary(0, 0, 0, 0, False, universe_metadata)

        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        current = current.astimezone(timezone.utc)
        current_ist = current.astimezone(IST)
        market_cfg = self.cfg.get("market", {})
        try:
            intraday_active = is_market_open(current_ist, market_cfg)
        except CalendarUnavailable as exc:
            universe_metadata.update({"calendar_available": False, "calendar_error": str(exc)})
            logger.error("Scan suppressed because exchange calendar is unavailable: %s", exc)
            return ScanSummary(len(stocks), 0, len(stocks), 0, False, universe_metadata)

        db = get_session()
        try:
            minimum = self.required_daily_candles()
            frames = self._load_cached_daily_frames(stocks, db)
            try:
                quotes = self.fetcher.get_full_market_quotes(
                    [stock.instrument_key for stock in stocks],
                    batch_size=int(self.cfg.get("upstox", {}).get("quote_batch_size", 100)),
                    allow_missing=True,
                )
            except Exception as exc:
                logger.error("Scan incomplete: batched quote sweep failed; no alerts sent: %s", exc)
                return ScanSummary(len(stocks), 0, len(stocks), 0, False, universe_metadata)

            quote_dates = set()
            skipped_reasons: dict[str, int] = {}
            quote_invalid: dict[str, str] = {}
            fresh_quotes = {}
            max_age = int(self.cfg.get("upstox", {}).get("max_quote_age_seconds", 300))
            for stock in stocks:
                quote = quotes.get(stock.instrument_key)
                if quote is None:
                    skipped_reasons["missing_quote"] = skipped_reasons.get("missing_quote", 0) + 1
                    continue
                trade_time, reason = _validate_quote(quote, current, max_age)
                if reason:
                    quote_invalid[stock.symbol] = reason
                    skipped_reasons[reason] = skipped_reasons.get(reason, 0) + 1
                    logger.warning("Skipping %s: invalid quote (%s)", stock.symbol, reason)
                    continue
                quote_dates.add(trade_time.astimezone(IST).date())
                fresh_quotes[stock.instrument_key] = quote
            if skipped_reasons:
                logger.warning(
                    "Quote validation skipped %s/%s symbols; reason counts=%s; examples=%s",
                    len(stocks) - len(fresh_quotes), len(stocks), skipped_reasons,
                    ", ".join(f"{symbol}:{reason}" for symbol, reason in list(quote_invalid.items())[:15]),
                )
            if not quote_dates:
                logger.error("Scan incomplete: quote response contained no usable OHLCV; no alerts sent.")
                return ScanSummary(len(stocks), 0, len(stocks), 0, False, universe_metadata)
            if len(quote_dates) != 1:
                logger.error("Scan incomplete: quote batches returned mixed trading dates; no alerts sent.")
                return ScanSummary(len(stocks), 0, len(stocks), 0, False, universe_metadata)
            session_date = next(iter(quote_dates))
            if intraday_active and session_date != current_ist.date():
                logger.error("Scan suppressed: live quote session %s does not match exchange date %s.",
                             session_date, current_ist.date())
                return ScanSummary(len(stocks), 0, len(stocks), 0, False, universe_metadata)

            # Keep canonical daily history separate from today's unfinished
            # session even if a stale/unverified same-date row exists in cache.
            valid_frames = {}
            invalid_history = set()
            for stock in stocks:
                frame = frames.get(stock.symbol)
                if frame is None or frame.empty:
                    valid_frames[stock.symbol] = pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
                    continue
                if not _valid_ohlcv_frame(frame):
                    invalid_history.add(stock.symbol)
                    skipped_reasons["invalid_cached_ohlcv"] = skipped_reasons.get("invalid_cached_ohlcv", 0) + 1
                    continue
                frame = frame.copy()
                dates = pd.to_datetime(frame["ts"], errors="coerce").map(
                    lambda value: _daily_timestamp(value).date() if not pd.isna(value) else None
                )
                valid_frames[stock.symbol] = frame.loc[dates < session_date].reset_index(drop=True)

            # The universe count is the denominator: selected stocks lacking
            # a fresh quote, valid cache, or enough history reduce coverage.
            tradability_cfg = self.universe_cfg.get("tradability", {}) or {}
            liquidity_metrics = {}
            tradable_candidates = []
            liquidity_excluded = {}
            if tradability_cfg.get("enabled", True):
                sessions = int(tradability_cfg.get("average_traded_value_sessions", 20))
                min_observations = int(tradability_cfg.get("minimum_observations", sessions))
                min_value = float(tradability_cfg.get("minimum_average_traded_value_inr", 10_000_000))
                unknown_policy = tradability_cfg.get("unknown_policy", "exclude")
                for stock in stocks:
                    metric = average_traded_value(
                        valid_frames.get(stock.symbol), sessions=sessions,
                        minimum_observations=min_observations,
                    )
                    liquidity_metrics[stock.symbol] = metric
                    if metric.get("status") != "available":
                        if unknown_policy == "include":
                            tradable_candidates.append(stock)
                        else:
                            liquidity_excluded[stock.symbol] = metric.get("reason", "liquidity_unavailable")
                    elif metric["average_inr"] >= min_value:
                        tradable_candidates.append(stock)
                    else:
                        liquidity_excluded[stock.symbol] = "below_average_traded_value_threshold"
            else:
                tradable_candidates = list(stocks)

            eligible_stocks = [
                stock for stock in tradable_candidates
                if stock.instrument_key in fresh_quotes
                and stock.symbol not in invalid_history
                and len(valid_frames.get(stock.symbol, ())) >= minimum
            ]
            insufficient_count = sum(
                stock.instrument_key in fresh_quotes and stock.symbol not in invalid_history
                and len(valid_frames.get(stock.symbol, ())) < minimum
                for stock in tradable_candidates
            )
            unavailable_count = sum(stock.instrument_key not in fresh_quotes for stock in tradable_candidates)
            invalid_candidate_history_count = sum(stock.symbol in invalid_history for stock in tradable_candidates)
            universe_metadata.update({
                "scan_eligible_count": len(eligible_stocks),
                "missing_or_stale_quote_count": unavailable_count,
                "insufficient_history_count": insufficient_count,
                "invalid_history_count": len(invalid_history),
                "tradability_enabled": bool(tradability_cfg.get("enabled", True)),
                "tradability_excluded_count": len(liquidity_excluded),
                "tradability_exclusion_reasons": {
                    reason: sum(value == reason for value in liquidity_excluded.values())
                    for reason in sorted(set(liquidity_excluded.values()))
                },
                "tradability_exclusion_samples": [
                    {"symbol": symbol, "reason": reason}
                    for symbol, reason in list(sorted(liquidity_excluded.items()))[:25]
                ],
                "average_traded_value_sessions": int(tradability_cfg.get("average_traded_value_sessions", 20)),
                "minimum_average_traded_value_inr": float(tradability_cfg.get("minimum_average_traded_value_inr", 10_000_000)),
                "final_tradable_count": len(tradable_candidates),
                "coverage_denominator": len(tradable_candidates),
                "coverage_threshold": float(self.cfg.get("scan", {}).get("min_coverage", 0.95)),
                "session_date": session_date.isoformat(),
                "provisional_intraday_enabled": intraday_active,
            })
            if insufficient_count:
                logger.warning(
                    "Scan has insufficient history for %s/%s stocks (<%s daily bars); "
                    "those stocks are skipped, eligible stock alerts will still be sent.",
                    insufficient_count, len(stocks), minimum,
                )

            # Evaluate the entire universe before writing signals or sending anything.
            # A detector exception therefore cannot produce a misleading partial sweep.
            evaluations = []
            failed = []
            def evaluate_paths(stock):
                completed = valid_frames[stock.symbol]
                daily_result, _ = self._scan_stock(stock, completed)
                results = []
                if daily_result:
                    results.append((daily_result, completed, False))
                if intraday_active:
                    quote = fresh_quotes[stock.instrument_key]
                    quote_bar = {
                        "ts": pd.Timestamp(session_date),
                        **{field: float(quote[field]) for field in ("open", "high", "low", "close", "volume")},
                    }
                    intraday = pd.concat([completed, pd.DataFrame([quote_bar])], ignore_index=True)
                    provisional_result, _ = self._scan_stock(stock, intraday, True)
                    if provisional_result:
                        provisional_result = {
                            **provisional_result,
                            "session_date": session_date.isoformat(),
                            "quote_timestamp": datetime.fromtimestamp(
                                int(quote["last_trade_time"]) / 1000, timezone.utc
                            ).isoformat(),
                            "bar_status": "provisional",
                        }
                        results.append((provisional_result, intraday, True))
                return results

            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = {
                    executor.submit(evaluate_paths, stock): stock
                for stock in eligible_stocks
                }
                for future in as_completed(futures):
                    stock = futures[future]
                    try:
                        evaluations.append((stock, future.result()))
                    except Exception as exc:
                        logger.error("Signal evaluation failed for %s: %s", stock.symbol, exc)
                        failed.append(stock.symbol)
            if failed:
                logger.error(
                    "Scan incomplete: %s/%s signal evaluations failed; no alerts sent.",
                    len(failed), len(stocks),
                )
                return ScanSummary(
                    len(stocks), len(eligible_stocks) - len(failed),
                    unavailable_count + insufficient_count + invalid_candidate_history_count + len(failed), 0, False,
                    universe_metadata,
                )

            # Alert deduplication follows the market-data session, not the host
            # calendar date (important for manual scans after the market closes).
            today = session_date
            evaluated_count = len(eligible_stocks)
            coverage = evaluated_count / len(tradable_candidates) if tradable_candidates else 0.0
            min_coverage = float(self.cfg.get("scan", {}).get("min_coverage", 0.95))
            if not 0 <= min_coverage <= 1:
                logger.error("Invalid scan.min_coverage %s; suppressing alerts.", min_coverage)
                return ScanSummary(len(stocks), evaluated_count, unavailable_count + insufficient_count,
                                   0, False, {**universe_metadata, "coverage_error": "threshold_out_of_range"})
            universe_metadata.update({"coverage_ratio": coverage, "coverage_percent": round(coverage * 100, 2)})
            if coverage < min_coverage:
                logger.error("Alert dispatch suppressed: coverage %.2f%% is below %.2f%% threshold.",
                             coverage * 100, min_coverage * 100)
                return ScanSummary(len(stocks), evaluated_count,
                                   unavailable_count + insufficient_count + invalid_candidate_history_count,
                                   0, False, universe_metadata)
            new_signal_count = 0
            destinations = [
                target for notifier in self.notifiers for target in notifier.delivery_targets()
            ]
            seed_legacy_deliveries(db, destinations)

            market_context_filtered = 0
            for stock, path_results in evaluations:
                for res, df, provisional in path_results:
                    try:
                        fund = self.fundamentals.get_fundamentals(stock.symbol, stock.isin or "")
                        try:
                            market_context = self._market_context_for(valid_frames[stock.symbol], fund)
                        except Exception as exc:
                            logger.info("Optional market context unavailable for %s: %s", stock.symbol, exc)
                            market_context = {"status": "unavailable", "reason": "context_error",
                                              "mode": self.cfg.get("market_context", {}).get("mode", "informational")}
                        try:
                            derivatives_context = self._derivatives_context_for(stock, today)
                        except Exception as exc:
                            logger.info("Optional derivatives context unavailable for %s: %s", stock.symbol, exc)
                            derivatives_context = {"status": "unavailable", "reason": "context_error"}
                        if market_context.get("mode") == "filter":
                            expected_regime = "bullish" if res["direction"] == "BUY" else "bearish"
                            regimes = [market_context.get("benchmark", {})]
                            if market_context.get("sector_key"):
                                regimes.append(market_context.get("sector_regime", {}))
                            unavailable_required = any(item.get("status") != "available" for item in regimes)
                            opposing = any(item.get("status") == "available" and item.get("regime") != expected_regime
                                           for item in regimes)
                            if opposing or (unavailable_required and self.cfg.get("market_context", {}).get("filter_unknown_policy", "exclude") == "exclude"):
                                market_context_filtered += 1
                                continue
                        signal_type = res["signal_type"]
                        quote = fresh_quotes[stock.instrument_key]
                        res = {**res, "price": float(quote["last_price"])}
                        existing = db.query(Signal).filter_by(
                            symbol=stock.symbol, signal_date=today, signal_type=signal_type
                        ).first()
                        old_score = int(existing.indicator_value or 0) if existing else 0
                        if existing and res["confluence_score"] <= old_score:
                            chart_path = existing.chart_path
                        else:
                            from charts.chart_generator import generate_signal_chart
                            chart_path = generate_signal_chart(
                                symbol=stock.symbol,
                                company_name=stock.company_name or stock.symbol,
                                df=df,
                                signal_type=signal_type,
                                direction=res["direction"],
                                indicator_value=float(res["confluence_score"]),
                            )

                        saved = upsert_signal(
                            session=db,
                            symbol=stock.symbol,
                            signal_date=today,
                            signal_type=signal_type,
                            direction=res["direction"],
                            price=res["price"],
                            indicator_value=float(res["confluence_score"]),
                            indicator_detail=json.dumps({
                                "signal_horizon": "provisional_intraday" if provisional else "confirmed_daily",
                                "bar_status": "provisional" if provisional else "confirmed_daily_cache",
                                "session_date": today.isoformat(),
                                "quote_timestamp": res.get("quote_timestamp"),
                                "confluence_score": res["confluence_score"],
                                "total_signals": res["total_signals"],
                                "group_votes": res.get("group_votes", {}),
                                "conflicting_groups": res.get("conflicting_groups", []),
                                "confluence_mode": res.get(
                                    "confluence_mode", getattr(self, "confluence_cfg", {}).get("mode", "crossover")
                                ),
                                "score_version": res.get("score_version", 1),
                                "checks": res["checks"],
                                "market_context": market_context,
                                "derivatives_context": derivatives_context,
                                "fundamental_provenance": {
                                    key: fund.get(key) for key in (
                                        "source_url", "fetched_at", "cache_age_hours", "freshness",
                                        "data_basis", "quarterly_data_status"
                                    )
                                } if fund else {"freshness": "unavailable"},
                                "average_traded_value": liquidity_metrics.get(stock.symbol),
                            }),
                            chart_path=chart_path,
                        )
                        saved_detail = json.loads(saved.indicator_detail or "{}")
                        saved_score = int(saved.indicator_value or 0)
                        saved_checks = saved_detail.get("checks", res["checks"])
                        saved_total = int(saved_detail.get("total_signals", res["total_signals"]))
                        payload = AlertPayload(
                            symbol=stock.symbol,
                            company_name=stock.company_name or stock.symbol,
                            signal_type=signal_type,
                            direction=res["direction"],
                            price=res["price"],
                            price_source="Upstox last_price",
                            quote_timestamp=datetime.fromtimestamp(
                                quote["last_trade_time"] / 1000, timezone.utc
                            ).isoformat(),
                            indicator_value=float(saved_score),
                            indicator_detail={"confluence_score": saved_score, "checks": saved_checks},
                            provisional=provisional,
                            signal_session_date=today.isoformat(),
                            bar_status="provisional" if provisional else "confirmed_daily_cache",
                            confluence_score=saved_score,
                            total_signals=saved_total,
                            confluence_mode=saved_detail.get(
                                "confluence_mode", res.get(
                                    "confluence_mode", getattr(self, "confluence_cfg", {}).get("mode", "crossover")
                                )
                            ),
                            checks=saved_checks,
                            chart_path=saved.chart_path,
                            mcap_cr=fund.get("mcap_cr") if fund else None,
                            cap_category=fund.get("cap_category") if fund else None,
                            sector=fund.get("sector") if fund else None,
                            industry=fund.get("industry") if fund else None,
                            pe_ratio=fund.get("pe_ratio") if fund else None,
                            pb_ratio=fund.get("pb_ratio") if fund else None,
                            roe=fund.get("roe") if fund else None,
                            profit_q1_cr=fund.get("profit_q1_cr") if fund else None,
                            profit_q2_cr=fund.get("profit_q2_cr") if fund else None,
                            profit_q3_cr=fund.get("profit_q3_cr") if fund else None,
                            profit_q4_cr=fund.get("profit_q4_cr") if fund else None,
                            profit_ttm_cr=fund.get("profit_ttm_cr") if fund else None,
                            promoter_holding_pct=fund.get("promoter_holding_pct") if fund else None,
                            screener_url=fund.get("screener_url") if fund else None,
                            fundamentals_fetched_at=fund.get("fetched_at") if fund else None,
                            fundamentals_cache_age_hours=fund.get("cache_age_hours") if fund else None,
                            fundamentals_freshness=fund.get("freshness", "unavailable") if fund else "unavailable",
                            fundamentals_data_basis=fund.get("data_basis", "unknown") if fund else "unknown",
                            market_regime=market_context.get("benchmark", {}).get("regime"),
                            market_relative_strength_pct=market_context.get("benchmark_relative_strength", {}).get("relative_strength_pct"),
                            sector_regime=market_context.get("sector_regime", {}).get("regime"),
                            sector_relative_strength_pct=market_context.get("sector_relative_strength", {}).get("relative_strength_pct"),
                            derivatives_context=derivatives_context,
                        )
                        if self._dispatch_alert(payload, db, saved.id):
                            new_signal_count += 1
                            logger.info(
                                "Confluence alert/update dispatched for %s: %s (%s/%s groups)",
                                stock.symbol, res["direction"], saved_score, saved_total,
                            )
                    except Exception as exc:
                        logger.error("Error processing alert for %s: %s", stock.symbol, exc, exc_info=True)

            universe_metadata["market_context_filtered_count"] = market_context_filtered

            logger.info(
                "Scan complete: mode=%s selected=%s source_counts=%s series_excluded=%s eligible=%s; "
                "%s/%s tradable candidates evaluated; %s alerts dispatched.",
                universe_metadata.get("mode"), universe_metadata.get("selected_count"),
                {key: universe_metadata.get(key) for key in ("constituent_count", "custom_count", "fno_count")},
                universe_metadata.get("series_exclusion_count"), len(eligible_stocks),
                len(eligible_stocks), len(tradable_candidates), new_signal_count,
            )
            return ScanSummary(
                len(stocks), evaluated_count, unavailable_count + insufficient_count + invalid_candidate_history_count,
                new_signal_count, evaluated_count == len(tradable_candidates),
                universe_metadata,
            )
        finally:
            db.close()

    def _dispatch_alert(self, payload: AlertPayload, db, signal_id: int) -> bool:
        """Send only score escalations, tracking success independently per target."""
        sent_any = False
        destinations = [
            target
            for notifier in self.notifiers
            for target in notifier.delivery_targets()
        ]
        for notifier in self.notifiers:
            for target in notifier.delivery_targets():
                previous_score = get_delivery_score(db, signal_id, target)
                if previous_score is not None and previous_score >= payload.confluence_score:
                    continue
                try:
                    if notifier.notify_target(payload, target, send_chart=True):
                        mark_delivery_sent(db, signal_id, target, payload.confluence_score)
                        sent_any = True
                    else:
                        logger.warning(f"Notifier {type(notifier).__name__} returned failure for {target}.")
                except Exception as e:
                    logger.error(f"Notifier {type(notifier).__name__} failed for {target}: {e}")
        refresh_signal_notified_state(db, signal_id, destinations)
        return sent_any
