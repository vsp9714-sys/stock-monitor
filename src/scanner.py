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
import inspect
import json
import logging
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, time as datetime_time, timedelta, timezone
import hashlib
from typing import Optional, Any
from zoneinfo import ZoneInfo

import pandas as pd
import numpy as np
from sqlalchemy import func, text

from src.config_loader import get_config
from src.db import (
    DailyCandleRefresh,
    MarketContextCache,
    OHLCVCache,
    PaperTrade,
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
from src.signals.ema_cross import detect_ema_crossover, compute_ema
from src.signals.macd import detect_macd_signal
from src.signals.rsi import detect_rsi_signal, compute_rsi
from src.signals.confluence import evaluate_confluence
from src.signals.volume_breakout import detect_volume_breakout, nse_expected_volume_fraction
from src.signals.adx import adx_filter
from src.paper_trading import record_paper_trade_entry, monitor_open_paper_trades, alert_type_for_tier
from src.universe import UniverseSelectionError, UniverseSelector
from src.market_calendar import (
    CalendarUnavailable,
    IST,
    is_valid_candle_day,
    is_market_open,
    is_trading_day,
    latest_completed_session_day,
)
from src.market_context import (
    average_traded_value,
    compute_universe_rs_ratings,
    regime_for_candles,
    relative_strength,
)
from src.derivatives import nearest_expiry, summarize_option_chain
from src.risk import risk_reference

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


def _json_default(value):
    """Convert common NumPy/pandas/date values to JSON-native primitives."""
    if value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if isinstance(value, set):
        return sorted(value, key=str)
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    return str(value)


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
        self.guardrails_cfg = self.cfg.get("guardrails", {})
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
                        if not is_valid_candle_day(candle_ts.date(), getattr(self, "cfg", {}).get("market", {})):
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
        allow_weekend: bool = False,
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
        if current.weekday() >= 5 and not allow_weekend:
            logger.info("Daily candle refresh skipped on weekend.")
            return {"universe_size": 0, "updated": 0, "failed": 0, "skipped": True}
        if target_session is None and current.time() < datetime_time(15, 45):
            logger.info("Daily candle refresh skipped before post-close finalization window.")
            return {"universe_size": 0, "updated": 0, "failed": 0, "skipped": True}
        target_day = target_session or current.date()
        if target_day > current.date():
            raise ValueError("Daily refresh target session cannot be in the future")
        try:
            if not is_trading_day(target_day, getattr(self, "cfg", {}).get("market", {})):
                return {"universe_size": 0, "updated": 0, "failed": 0, "skipped": True,
                        "reason": "target_not_trading_day", "session_date": target_day.isoformat()}
        except CalendarUnavailable as exc:
            logger.error("Daily refresh skipped for unknown target session: %s", exc)
            return {"universe_size": 0, "updated": 0, "failed": 0, "skipped": True,
                    "reason": "calendar_unavailable", "session_date": target_day.isoformat()}

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
            latest_by_symbol = {}
            for symbol, cached_ts in (
                session.query(OHLCVCache.symbol, OHLCVCache.ts)
                .filter(OHLCVCache.interval == "1d")
                .order_by(OHLCVCache.symbol, OHLCVCache.ts.desc())
                .yield_per(10000)
            ):
                if symbol in latest_by_symbol:
                    continue
                cached_day = pd.Timestamp(cached_ts).date()
                if is_valid_candle_day(cached_day, getattr(self, "cfg", {}).get("market", {})):
                    latest_by_symbol[symbol] = cached_ts
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
                        if ts.date() > target_day or not is_valid_candle_day(
                            ts.date(), getattr(self, "cfg", {}).get("market", {})
                        ):
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
        """Probe Upstox during the scheduled 08:00 IST window, then refresh the latest completed session.

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
            not manual and not (datetime_time(8, 0) <= current.time() < datetime_time(8, 30))
        ):
            logger.info("Morning daily-candle readiness check skipped outside its weekday 08:00-08:30 window.")
            return {"universe_size": 0, "updated": 0, "failed": 0, "skipped": True}

        stocks = {stock.symbol: stock for stock in self.get_active_stocks()}
        probe_symbols = ("RELIANCE", "TCS", "360ONE")
        missing = [symbol for symbol in probe_symbols if symbol not in stocks]
        if missing:
            logger.warning("Daily refresh readiness probe missing active symbols: %s", missing)
            return {"universe_size": len(stocks), "updated": 0, "failed": 0, "skipped": True,
                    "reason": "probe_symbols_missing"}

        probe_dates = []
        expected_session = latest_completed_session_day(current, getattr(self, "cfg", {}).get("market", {}))
        probe_from = (current.date() - timedelta(days=14)).isoformat()
        for symbol in probe_symbols:
            stock = stocks[symbol]
            candles = self.fetcher.get_historical_candles(
                stock.instrument_key, "day", from_date=probe_from, to_date=current.date().isoformat()
            )
            if candles.empty:
                logger.info("Daily refresh not ready: provider returned no recent candles for %s", symbol)
                return {"universe_size": len(stocks), "updated": 0, "failed": 0, "skipped": True,
                        "reason": "provider_not_ready", "probe_symbol": symbol}
            valid_dates = [
                _daily_timestamp(row.ts).date()
                for row in candles.itertuples(index=False)
                if _daily_timestamp(row.ts).date() <= expected_session
                and is_valid_candle_day(_daily_timestamp(row.ts).date(), getattr(self, "cfg", {}).get("market", {}))
            ]
            if not valid_dates:
                logger.info(
                    "Daily refresh not ready: no valid completed NSE-session candles for %s; expected at most %s",
                    symbol, expected_session,
                )
                return {"universe_size": len(stocks), "updated": 0, "failed": 0, "skipped": True,
                        "reason": "provider_not_ready", "probe_symbol": symbol,
                        "expected_session": expected_session.isoformat()}
            probe_dates.append(max(valid_dates))

        if len(set(probe_dates)) != 1:
            logger.info("Daily refresh not ready: probe symbols disagree on latest session dates: %s", probe_dates)
            return {"universe_size": len(stocks), "updated": 0, "failed": 0, "skipped": True,
                    "reason": "probe_dates_disagree", "probe_dates": [d.isoformat() for d in probe_dates]}

        target = probe_dates[0]
        if target < expected_session:
            logger.info(
                "Daily refresh not ready: latest valid probe session %s is older than expected completed session %s",
                target, expected_session,
            )
            return {"universe_size": len(stocks), "updated": 0, "failed": 0, "skipped": True,
                    "reason": "provider_not_ready", "session_date": target.isoformat(),
                    "expected_session": expected_session.isoformat()}
        session = get_session()
        try:
            cached_dates = [
                pd.Timestamp(value).date()
                for (value,) in session.query(OHLCVCache.ts).filter(OHLCVCache.interval == "1d").distinct().all()
                if value is not None
            ]
            valid_cached_dates = [
                cached_day for cached_day in cached_dates
                if is_valid_candle_day(cached_day, getattr(self, "cfg", {}).get("market", {}))
            ]
            cached_latest = max(valid_cached_dates, default=None)
            if cached_latest is not None and target < cached_latest:
                logger.warning("Daily refresh probe date %s is behind cache date %s; refusing regression.",
                               target, cached_latest)
                return {"universe_size": len(stocks), "updated": 0, "failed": 0, "skipped": True,
                        "reason": "provider_behind_cache", "session_date": target.isoformat()}
        finally:
            session.close()

        logger.info("Daily refresh readiness confirmed for session %s; starting paced full-universe refresh.", target)
        return self.refresh_completed_daily_cache(
            now=current, checkpoint_symbols=checkpoint_symbols, target_session=target,
            allow_weekend=manual,
        )

    # ─── Single Stock Scan & Confluence Evaluation ────────────────────────

    def _scan_stock(
        self,
        stock: Stock,
        df: pd.DataFrame,
        provisional: bool = False,
        current_time: Optional[datetime] = None,
        is_power_hour: bool = False,
        diagnostic: bool = False,
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
            rsi_val = None

            # ── 1. RSI Check ────────────────────────────────────────────
            if "rsi" in self.signal_cfg and self.signal_cfg["rsi"].get("enabled", True):
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
            if "macd" in self.signal_cfg and self.signal_cfg["macd"].get("enabled", True):
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
            if "ema_crossover" in self.signal_cfg and self.signal_cfg["ema_crossover"].get("enabled", True):
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
            if "volume_breakout" in self.signal_cfg and self.signal_cfg["volume_breakout"].get("enabled", True):
                vol_cfg = self.signal_cfg["volume_breakout"]
                vol_pace_cfg = getattr(self, "guardrails_cfg", {}).get("volume_pace", {})
                use_pace = vol_pace_cfg.get("enabled", True)
                min_floor = float(vol_pace_cfg.get("min_fraction_floor", 0.05))
                frac = (
                    nse_expected_volume_fraction(current_time, min_floor=min_floor)
                    if (provisional and use_pace) else None
                )
                vol_result = detect_volume_breakout(
                    stock.symbol, df,
                    volume_multiplier=vol_cfg.get("volume_multiplier", 2.0),
                    lookback_days=vol_cfg.get("breakout_lookback_days", 20),
                    expected_fraction=frac,
                    min_fraction_floor=min_floor,
                )
                vol_ratio = vol_result.volume_ratio
                raw_vol = getattr(vol_result, "raw_volume_ratio", vol_ratio) or vol_ratio
                if provisional and frac is not None and frac < 0.95:
                    value_text = f"{vol_ratio:.1f}× Pace"
                    pace_desc_tag = f"R-Vol {vol_ratio:.1f}× pace"
                else:
                    value_text = f"{vol_ratio:.1f}× Avg Vol"
                    pace_desc_tag = f"{vol_ratio:.1f}× Avg Vol"

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
                            "value_text": value_text,
                            "description": (
                                ("Bullish Breakout" if vol_result.direction == "BUY" else "Bearish Breakdown")
                                + (f" ({pace_desc_tag}; partial-session volume vs full-day history)" if provisional else "")
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
                            "value_text": value_text,
                            "description": (
                                "Spike (No Price Breakout)"
                                + (f" ({pace_desc_tag}; partial-session volume vs full-day history)" if provisional else "")
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
                        "value_text": value_text,
                        "description": (
                            f"Normal Volume ({pace_desc_tag}; partial-session volume vs full-day history)"
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

            # ── Guardrails & Filters ────────────────────────────────────
            guardrails_cfg = getattr(self, "guardrails_cfg", {})

            # ── Macro-Trend Gate (Close > EMA 50 > EMA 200) ───────────────
            trend_gate_cfg = guardrails_cfg.get("trend_gate", {})
            trend_gate_info = None
            if trend_gate_cfg.get("enabled", False):
                apply_trend = (
                    (provisional and trend_gate_cfg.get("apply_to_provisional", True)) or
                    (not provisional and trend_gate_cfg.get("apply_to_daily", True))
                )
                if apply_trend:
                    fast_p = int(trend_gate_cfg.get("fast_ema", 50))
                    slow_p = int(trend_gate_cfg.get("slow_ema", 200))
                    req_price_fast = bool(trend_gate_cfg.get("require_price_above_fast", True))
                    req_fast_slow = bool(trend_gate_cfg.get("require_fast_above_slow", True))
                    fail_closed = bool(trend_gate_cfg.get("fail_closed_on_short_history", False))
                    closes = df["close"].astype(float)
                    if len(df) < slow_p + 10:
                        if fail_closed:
                            logger.debug(
                                "%s: %s rejected by trend gate (insufficient history %d < %d)",
                                stock.symbol, consensus_dir, len(df), slow_p + 10,
                            )
                            return None, df
                        trend_gate_info = {
                            "status": "unavailable",
                            "reason": "insufficient_history",
                            "passed": True,
                        }
                    else:
                        ema_fast_s = compute_ema(closes, fast_p)
                        ema_slow_s = compute_ema(closes, slow_p)
                        ema_fast = float(ema_fast_s.iloc[-1])
                        ema_slow = float(ema_slow_s.iloc[-1])
                        passed_trend = True
                        if consensus_dir == "BUY":
                            if req_price_fast and current_price <= ema_fast:
                                passed_trend = False
                            if req_fast_slow and ema_fast <= ema_slow:
                                passed_trend = False
                        elif consensus_dir == "SELL":
                            if req_price_fast and current_price >= ema_fast:
                                passed_trend = False
                            if req_fast_slow and ema_fast >= ema_slow:
                                passed_trend = False

                        trend_gate_info = {
                            "status": "available",
                            "passed": passed_trend,
                            "ema_fast": round(ema_fast, 2),
                            "ema_slow": round(ema_slow, 2),
                            "fast_period": fast_p,
                            "slow_period": slow_p,
                        }
                        if not passed_trend:
                            logger.debug(
                                "%s: %s rejected by trend gate (price %.2f, EMA%d %.2f, EMA%d %.2f)",
                                stock.symbol, consensus_dir, current_price, fast_p, ema_fast, slow_p, ema_slow,
                            )
                            if not diagnostic:
                                return None, df

            # ── RSI Exhaustion Ceiling ──────────────────────────────────
            rsi_ex_cfg = guardrails_cfg.get("rsi_exhaustion", {})
            rsi_exhaustion_info = None
            if rsi_ex_cfg.get("enabled", False):
                apply_rsi_ex = (
                    (provisional and rsi_ex_cfg.get("apply_to_provisional", True)) or
                    (not provisional and rsi_ex_cfg.get("apply_to_daily", True))
                )
                if apply_rsi_ex:
                    max_rsi_buy = float(rsi_ex_cfg.get("max_rsi_buy", 68.0))
                    min_rsi_sell = float(rsi_ex_cfg.get("min_rsi_sell", 32.0))
                    val_to_check = None
                    if rsi_val is not None and pd.notna(rsi_val):
                        val_to_check = float(rsi_val)
                    else:
                        rsi_p = int(self.signal_cfg.get("rsi", {}).get("period", 14))
                        if len(df) >= rsi_p + 1:
                            rsi_series = compute_rsi(df["close"].astype(float), rsi_p)
                            if not rsi_series.empty and pd.notna(rsi_series.iloc[-1]):
                                val_to_check = float(rsi_series.iloc[-1])

                    if val_to_check is not None:
                        passed_rsi_ex = True
                        if consensus_dir == "BUY" and val_to_check > max_rsi_buy:
                            passed_rsi_ex = False
                        elif consensus_dir == "SELL" and val_to_check < min_rsi_sell:
                            passed_rsi_ex = False
                        rsi_exhaustion_info = {
                            "passed": passed_rsi_ex,
                            "rsi": round(val_to_check, 2),
                            "max_rsi_buy": max_rsi_buy,
                            "min_rsi_sell": min_rsi_sell,
                        }
                        if not passed_rsi_ex:
                            logger.debug(
                                "%s: %s rejected by RSI exhaustion (RSI %.2f)",
                                stock.symbol, consensus_dir, val_to_check,
                            )
                            if not diagnostic:
                                return None, df

            # ── Wick Rejection Filter (Close Location Value / CLV) ───────
            wick_cfg = guardrails_cfg.get("wick_filter", {})
            clv = None
            if "high" in df.columns and "low" in df.columns and "close" in df.columns:
                last_bar = df.iloc[-1]
                day_high = float(last_bar["high"])
                day_low = float(last_bar["low"])
                day_close = float(last_bar["close"])
                day_range = day_high - day_low
                if day_range > 0:
                    clv = (day_close - day_low) / day_range
                else:
                    clv = 0.5

            apply_wick = wick_cfg.get("enabled", True) and (
                (provisional and wick_cfg.get("apply_to_provisional", True)) or
                (not provisional and wick_cfg.get("apply_to_daily", False))
            )
            wick_filter_info = None
            if clv is not None:
                min_buy_clv = float(wick_cfg.get("min_clv_buy", 0.70))
                max_sell_clv = float(wick_cfg.get("max_clv_sell", 0.30))
                passed_wick = True
                if apply_wick:
                    if consensus_dir == "BUY" and clv < min_buy_clv:
                        passed_wick = False
                        logger.debug(
                            "%s: BUY signal rejected by wick filter (CLV %.2f < %.2f; upper wick rejection)",
                            stock.symbol, clv, min_buy_clv,
                        )
                        if not diagnostic:
                            return None, df
                    elif consensus_dir == "SELL" and clv > max_sell_clv:
                        passed_wick = False
                        logger.debug(
                            "%s: SELL signal rejected by wick filter (CLV %.2f > %.2f; lower wick rejection)",
                            stock.symbol, clv, max_sell_clv,
                        )
                        if not diagnostic:
                            return None, df
                wick_filter_info = {
                    "enabled": wick_cfg.get("enabled", True),
                    "apply_to_daily": wick_cfg.get("apply_to_daily", False),
                    "apply_to_provisional": wick_cfg.get("apply_to_provisional", True),
                    "clv": round(clv, 2),
                    "passed": passed_wick,
                    "min_clv_buy": min_buy_clv,
                    "max_clv_sell": max_sell_clv,
                }

            # ── ADX Trend Strength Filter ───────────────────────────────
            adx_cfg = guardrails_cfg.get("adx_filter", {})
            adx_info = None
            if adx_cfg.get("enabled", True):
                apply_adx = (
                    (provisional and adx_cfg.get("apply_to_provisional", True)) or
                    (not provisional and adx_cfg.get("apply_to_daily", True))
                )
                if apply_adx:
                    adx_info = adx_filter(
                        df,
                        period=int(adx_cfg.get("period", 14)),
                        min_adx=float(adx_cfg.get("min_adx", 20.0)),
                        strong_threshold=float(adx_cfg.get("strong_trend_threshold", 25.0)),
                        direction=consensus_dir,
                        require_di_alignment=bool(adx_cfg.get("require_di_alignment", True)),
                    )
                    if not adx_info.get("passed", True):
                        logger.debug(
                            "%s: %s signal rejected by ADX filter (ADX %.1f < %.1f; %s market / no trend)",
                            stock.symbol, consensus_dir, adx_info.get("adx") or 0.0,
                            float(adx_cfg.get("min_adx", 20.0)), adx_info.get("trend_strength", "weak"),
                        )
                        if not diagnostic:
                            return None, df

            # Determine signal type, tier, and horizon
            if provisional:
                if is_power_hour:
                    signal_type = f"power_hour_confluence_{consensus_dir.lower()}"
                    alert_tier = "power_hour_confirmed"
                    signal_horizon = "power_hour"
                else:
                    signal_type = f"provisional_intraday_confluence_{consensus_dir.lower()}"
                    alert_tier = "intraday_radar"
                    signal_horizon = "provisional_intraday"
            else:
                signal_type = f"confluence_{consensus_dir.lower()}"
                alert_tier = "confirmed_daily"
                signal_horizon = "confirmed_daily"

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
                "signal_type": signal_type,
                "signal_horizon": signal_horizon,
                "alert_tier": alert_tier,
                "clv": round(clv, 2) if clv is not None else None,
                "adx": adx_info,
                "trend_gate": trend_gate_info,
                "rsi_exhaustion": rsi_exhaustion_info,
                "wick_filter": wick_filter_info,
                "passed_stock_guardrails": (
                    (trend_gate_info is None or trend_gate_info.get("passed", True))
                    and (rsi_exhaustion_info is None or rsi_exhaustion_info.get("passed", True))
                    and (wick_filter_info is None or wick_filter_info.get("passed", True))
                    and (adx_info is None or adx_info.get("passed", True))
                ),
            }
            return result, df

        except Exception as e:
            logger.error(f"Error scanning {stock.symbol}: {e}", exc_info=False)
            raise

    def _call_scan_stock(
        self,
        stock: Stock,
        df: pd.DataFrame,
        provisional: bool = False,
        current_time: Optional[datetime] = None,
        is_power_hour: bool = False,
        diagnostic: bool = False,
    ) -> tuple[Optional[dict], pd.DataFrame]:
        """Safely call _scan_stock, adapting to legacy or test mocks with fewer parameters."""
        sig = inspect.signature(self._scan_stock)
        params = sig.parameters
        has_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        param_names = set(params.keys())

        kwargs = {}
        if has_varkw or "provisional" in param_names:
            kwargs["provisional"] = provisional
        if has_varkw or "current_time" in param_names:
            kwargs["current_time"] = current_time
        if has_varkw or "is_power_hour" in param_names:
            kwargs["is_power_hour"] = is_power_hour
        if has_varkw or "diagnostic" in param_names:
            kwargs["diagnostic"] = diagnostic

        return self._scan_stock(stock, df, **kwargs)

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
                if not is_valid_candle_day(
                    _daily_timestamp(row.ts).date(), getattr(self, "cfg", {}).get("market", {})
                ):
                    continue
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
        ph_cfg = getattr(self, "guardrails_cfg", {}).get("two_tier_alerts", {})
        ph_enabled = ph_cfg.get("enabled", True)
        ph_start = ph_cfg.get("power_hour_start", "15:10")
        try:
            ph_h, ph_m = map(int, ph_start.split(":"))
        except Exception:
            ph_h, ph_m = 15, 10
        try:
            intraday_active = is_market_open(current_ist, market_cfg)
        except CalendarUnavailable as exc:
            universe_metadata.update({"calendar_available": False, "calendar_error": str(exc)})
            logger.error("Scan suppressed because exchange calendar is unavailable: %s", exc)
            return ScanSummary(len(stocks), 0, len(stocks), 0, False, universe_metadata)

        is_power_hour = ph_enabled and intraday_active and (
            current_ist.hour > ph_h or (current_ist.hour == ph_h and current_ist.minute >= ph_m)
        )

        db = get_session()
        try:
            minimum = self.required_daily_candles()
            frames = self._load_cached_daily_frames(stocks, db)

            # Ensure open paper trade symbols also get quotes even if not in current universe
            open_pt_symbols = []
            try:
                q = db.query(PaperTrade.symbol).filter_by(status="open")
                if hasattr(q, "distinct"):
                    q = q.distinct()
                if hasattr(q, "all"):
                    open_pt_symbols = [row[0] for row in q.all()]
            except Exception:
                open_pt_symbols = []

            universe_symbols = {s.symbol for s in stocks}
            missing_pt_symbols = [sym for sym in open_pt_symbols if sym not in universe_symbols]
            extra_pt_stocks = []
            if missing_pt_symbols:
                try:
                    extra_pt_stocks = db.query(Stock).filter(Stock.symbol.in_(missing_pt_symbols)).all()
                except Exception:
                    extra_pt_stocks = []
            all_quote_stocks = list(stocks) + list(extra_pt_stocks)
            keys_to_fetch = list({s.instrument_key for s in all_quote_stocks if getattr(s, "instrument_key", None)})

            try:
                quotes = self.fetcher.get_full_market_quotes(
                    keys_to_fetch,
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
            for stock in extra_pt_stocks:
                quote = quotes.get(stock.instrument_key)
                if quote is not None:
                    _, r = _validate_quote(quote, current, max_age)
                    if not r:
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
            # No policy block means a legacy caller/config is unchanged. The
            # shipped config opts into the liquidity screen explicitly.
            universe_cfg = getattr(self, "universe_cfg", {}) or {}
            tradability_cfg = universe_cfg.get("tradability", {}) or {}
            tradability_enabled = bool(tradability_cfg.get("enabled", False))
            liquidity_metrics = {}
            tradable_candidates = []
            liquidity_excluded = {}
            if tradability_enabled:
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

            # Precompute cross-sectional Relative Strength Ratings across valid_frames vs benchmark
            guardrails_cfg = getattr(self, "guardrails_cfg", {}) or self.cfg.get("guardrails", {})
            rs_filter_cfg = guardrails_cfg.get("rs_filter", {})
            benchmark_key = self.cfg.get("market_context", {}).get("benchmark_key", "NSE_INDEX|Nifty 50")
            benchmark_frame = self._get_market_index_frame(benchmark_key, session_date - timedelta(days=1))
            rs_ratings = (
                compute_universe_rs_ratings(
                    valid_frames,
                    benchmark_frame,
                    as_of_date=session_date - timedelta(days=1),
                    lookback_weights=rs_filter_cfg.get("lookback_weights"),
                )
                if benchmark_frame is not None
                else {}
            )

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
                "tradability_enabled": tradability_enabled,
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
                daily_result, _ = self._call_scan_stock(
                    stock, completed, provisional=False, current_time=current, is_power_hour=is_power_hour
                )
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
                    provisional_result, _ = self._call_scan_stock(
                        stock, intraday, provisional=True, current_time=current, is_power_hour=is_power_hour
                    )
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
            surviving_candidates = []
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
                        stock_rs = rs_ratings.get(stock.symbol)
                        market_context["rs_rating"] = stock_rs
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
                        elif market_context.get("mode") == "benchmark_filter":
                            m_cfg = self.cfg.get("market_context", {})
                            allowed_buy = m_cfg.get("allowed_buy_regimes", ["bullish", "mixed"])
                            allowed_sell = m_cfg.get("allowed_sell_regimes", ["bearish", "mixed"])
                            expected_allowed = allowed_buy if res["direction"] == "BUY" else allowed_sell
                            bench = market_context.get("benchmark", {})
                            if bench.get("status") != "available":
                                if m_cfg.get("filter_unknown_policy", "exclude") == "exclude":
                                    market_context_filtered += 1
                                    continue
                            elif bench.get("regime") not in expected_allowed:
                                market_context_filtered += 1
                                continue

                        # Cross-sectional RS Filter check
                        if rs_filter_cfg.get("enabled", False):
                            apply_rs = (
                                (provisional and rs_filter_cfg.get("apply_to_provisional", True)) or
                                (not provisional and rs_filter_cfg.get("apply_to_daily", True))
                            )
                            if apply_rs:
                                min_buy = int(rs_filter_cfg.get("min_rs_rating_buy", 60))
                                max_sell = int(rs_filter_cfg.get("max_rs_rating_sell", 40))
                                unknown_policy = rs_filter_cfg.get("unknown_policy", "exclude")
                                if stock_rs is None:
                                    if unknown_policy == "exclude":
                                        continue
                                else:
                                    if res["direction"] == "BUY" and stock_rs < min_buy:
                                        continue
                                    if res["direction"] == "SELL" and stock_rs > max_sell:
                                        continue

                        surviving_candidates.append({
                            "stock": stock,
                            "res": res,
                            "df": df,
                            "provisional": provisional,
                            "fund": fund,
                            "market_context": market_context,
                            "stock_rs": stock_rs,
                            "derivatives_context": derivatives_context,
                        })
                    except Exception as exc:
                        logger.error("Error evaluating candidate for %s: %s", stock.symbol, exc, exc_info=True)

            universe_metadata["market_context_filtered_count"] = market_context_filtered
            universe_metadata.setdefault("risk_distance_filtered_count", 0)
            # Sort surviving candidates by (-rs_rating, -confluence_score, symbol)
            surviving_candidates.sort(
                key=lambda c: (
                    -(c["stock_rs"] if c["stock_rs"] is not None else -1),
                    -c["res"].get("confluence_score", 0),
                    c["stock"].symbol,
                )
            )

            for cand in surviving_candidates:
                stock = cand["stock"]
                res = cand["res"]
                df = cand["df"]
                provisional = cand["provisional"]
                fund = cand["fund"]
                market_context = cand["market_context"]
                stock_rs = cand["stock_rs"]
                derivatives_context = cand["derivatives_context"]
                try:
                    signal_type = res["signal_type"]
                    quote = fresh_quotes[stock.instrument_key]
                    res = {**res, "price": float(quote["last_price"])}
                    risk_context = risk_reference(
                        valid_frames[stock.symbol], entry_price=float(quote["last_price"]),
                        direction=res["direction"], config=self.cfg.get("risk", {}),
                    )
                    if risk_context.get("reason") == "risk_distance_exceeds_max":
                        universe_metadata["risk_distance_filtered_count"] = (
                            universe_metadata.get("risk_distance_filtered_count", 0) + 1
                        )
                        logger.info(
                            "Candidate %s suppressed: risk distance %.2f%% exceeds cap %.2f%%",
                            stock.symbol,
                            float(risk_context.get("risk_distance_pct") or 0.0),
                            float(risk_context.get("max_risk_distance_pct") or 0.0),
                        )
                        continue
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
                            "signal_horizon": res.get("signal_horizon", "provisional_intraday" if provisional else "confirmed_daily"),
                            "alert_tier": res.get("alert_tier", "confirmed_daily"),
                            "clv": res.get("clv"),
                            "adx": res.get("adx"),
                            "trend_gate": res.get("trend_gate"),
                            "rsi_exhaustion": res.get("rsi_exhaustion"),
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
                            "risk_context": risk_context,
                            "fundamental_provenance": {
                                key: fund.get(key) for key in (
                                    "source_url", "fetched_at", "cache_age_hours", "freshness",
                                    "data_basis", "quarterly_data_status"
                                )
                            } if fund else {"freshness": "unavailable"},
                            "average_traded_value": liquidity_metrics.get(stock.symbol),
                        }, default=_json_default),
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
                        indicator_detail={
                            "confluence_score": saved_score,
                            "checks": saved_checks,
                            "clv": res.get("clv"),
                            "adx": res.get("adx"),
                            "alert_tier": res.get("alert_tier", "confirmed_daily"),
                        },
                        provisional=provisional,
                        alert_tier=res.get("alert_tier", "confirmed_daily"),
                        clv=res.get("clv"),
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
                        rs_rating=stock_rs,
                        derivatives_context=derivatives_context,
                        risk_context=risk_context,
                    )
                    if self._dispatch_alert(payload, db, saved.id):
                        new_signal_count += 1
                        logger.info(
                            "Confluence alert/update dispatched for %s: %s (%s/%s groups)",
                            stock.symbol, res["direction"], saved_score, saved_total,
                        )
                        try:
                            record_paper_trade_entry(
                                session=db,
                                signal_id=saved.id,
                                symbol=stock.symbol,
                                direction=res["direction"],
                                entry_price=res["price"],
                                risk_ref=risk_context,
                                confluence_result=res,
                                alert_tier=res.get("alert_tier", "confirmed_daily"),
                                session_date=today,
                                config=self.cfg,
                                alert_type=alert_type_for_tier(res.get("alert_tier")),
                                rs_rating=stock_rs,
                            )
                        except Exception as pe:
                            logger.error("Failed to record paper trade entry for %s: %s", stock.symbol, pe)
                except Exception as exc:
                    logger.error("Error processing alert for %s: %s", stock.symbol, exc, exc_info=True)

            universe_metadata["market_context_filtered_count"] = market_context_filtered
            universe_metadata.setdefault("risk_distance_filtered_count", 0)
            # Monitor open paper trades during every scan cycle
            try:
                quotes_by_symbol = {
                    s.symbol: fresh_quotes[s.instrument_key]
                    for s in all_quote_stocks
                    if s.instrument_key in fresh_quotes
                }
                monitor_open_paper_trades(
                    session=db,
                    quotes_by_symbol=quotes_by_symbol,
                    notifiers=self.notifiers,
                    config=self.cfg,
                    session_date=today,
                )
            except Exception as pe:
                logger.error("Paper trade monitoring failed: %s", pe, exc_info=True)

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

    def audit_daily_candidates(
        self,
        session_date: Optional[date] = None,
        completed_frames: Optional[dict[str, pd.DataFrame]] = None,
        active_stocks: Optional[list[Stock]] = None,
    ) -> dict[str, Any]:
        """
        Deterministic post-market diagnostic evaluation across the entire universe
        using completed daily candles for session_date. Zero live alerts, zero paper trades.
        """
        session_date = session_date or date.today()
        stocks = list(active_stocks if active_stocks is not None else self.get_active_stocks())
        db = get_session()
        try:
            # 1. Load completed daily frames
            if completed_frames is not None:
                valid_frames = {s.symbol: completed_frames[s.symbol].copy() for s in stocks if s.symbol in completed_frames}
            else:
                valid_frames = self._load_cached_daily_frames(stocks, db)
                has_session = any(
                    not df.empty and pd.to_datetime(df["ts"].iloc[-1]).date() >= session_date
                    for df in valid_frames.values()
                )
                if not has_session and session_date == date.today():
                    keys = [s.instrument_key for s in stocks if getattr(s, "instrument_key", None)]
                    try:
                        raw_quotes = self.fetcher.get_full_market_quotes(keys, allow_missing=True)
                        for s in stocks:
                            q = raw_quotes.get(s.instrument_key)
                            if q and q.get("close") and q.get("high") and q.get("low"):
                                quote_bar = {
                                    "ts": pd.Timestamp(session_date),
                                    "open": float(q.get("open") or q.get("close")),
                                    "high": float(q["high"]),
                                    "low": float(q["low"]),
                                    "close": float(q["close"]),
                                    "volume": float(q.get("volume") or 0),
                                }
                                if s.symbol in valid_frames:
                                    valid_frames[s.symbol] = pd.concat(
                                        [valid_frames[s.symbol], pd.DataFrame([quote_bar])],
                                        ignore_index=True,
                                    )
                    except Exception as exc:
                        logger.warning("Quote fetch during audit fallback failed: %s", exc)

            # Filter each frame to ts <= session_date
            for sym in list(valid_frames.keys()):
                df = valid_frames[sym]
                if not df.empty:
                    valid_frames[sym] = df.loc[
                        df["ts"].map(lambda v: _daily_timestamp(v).date() <= session_date)
                    ].reset_index(drop=True)

            # 2. Universe and Tradability Screening
            minimum = self.required_daily_candles()
            universe_cfg = getattr(self, "universe_cfg", {}) or {}
            tradability_cfg = universe_cfg.get("tradability", {}) or {}
            tradability_enabled = bool(tradability_cfg.get("enabled", False))
            sessions = int(tradability_cfg.get("average_traded_value_sessions", 20))
            min_obs = int(tradability_cfg.get("minimum_observations", sessions))
            min_val = float(tradability_cfg.get("minimum_average_traded_value_inr", 10_000_000))
            unknown_policy = tradability_cfg.get("unknown_policy", "exclude")

            tradable_candidates = []
            liquidity_excluded = {}
            for stock in stocks:
                if not tradability_enabled:
                    tradable_candidates.append(stock)
                else:
                    metric = average_traded_value(
                        valid_frames.get(stock.symbol), sessions=sessions,
                        minimum_observations=min_obs,
                    )
                    if metric.get("status") != "available":
                        if unknown_policy == "include":
                            tradable_candidates.append(stock)
                        else:
                            liquidity_excluded[stock.symbol] = metric.get("reason", "liquidity_unavailable")
                    elif metric.get("average_inr", 0) >= min_val:
                        tradable_candidates.append(stock)
                    else:
                        liquidity_excluded[stock.symbol] = "below_average_traded_value_threshold"

            eligible_stocks = [
                s for s in tradable_candidates
                if s.symbol in valid_frames and len(valid_frames[s.symbol]) >= minimum
            ]
            failed_symbols = [
                s.symbol for s in stocks
                if s.symbol not in valid_frames or len(valid_frames[s.symbol]) < minimum
            ]
            universe_size = len(stocks)
            eligible_count = len(tradable_candidates)
            evaluated_count = len(eligible_stocks)
            coverage_ratio = evaluated_count / eligible_count if eligible_count else 0.0
            coverage_percent = round(coverage_ratio * 100, 2)
            min_coverage = float(self.cfg.get("scan", {}).get("min_coverage", 0.95))
            coverage_healthy = coverage_ratio >= min_coverage

            # 3. Benchmark Context & Regime Gate
            benchmark_key = self.cfg.get("market_context", {}).get("benchmark_key", "NSE_INDEX|Nifty 50")
            benchmark_frame = self._get_market_index_frame(benchmark_key, session_date)
            m_cfg = self.cfg.get("market_context", {}) or {}
            b_short = int(m_cfg.get("short_sma_sessions", 20))
            b_long = int(m_cfg.get("long_sma_sessions", 50))
            if benchmark_frame is not None and not benchmark_frame.empty:
                regime_data = regime_for_candles(benchmark_frame, short_window=b_short, long_window=b_long)
                bench_close = float(regime_data.get("close") or 0.0)
                bench_sma20 = float(regime_data.get("short_sma") or 0.0)
                bench_sma50 = float(regime_data.get("long_sma") or 0.0)
                bench_regime = str(regime_data.get("regime") or "unknown")
            else:
                bench_close, bench_sma20, bench_sma50, bench_regime = 0.0, 0.0, 0.0, "unknown"

            allowed_buy_regimes = m_cfg.get("allowed_buy_regimes", ["bullish", "mixed"])
            allowed_sell_regimes = m_cfg.get("allowed_sell_regimes", ["bearish", "mixed"])
            buy_regime_allowed = bench_regime in allowed_buy_regimes
            sell_regime_allowed = bench_regime in allowed_sell_regimes

            # 4. Cross-Sectional RS Ratings
            guardrails_cfg = getattr(self, "guardrails_cfg", {}) or self.cfg.get("guardrails", {}) or {}
            rs_filter_cfg = guardrails_cfg.get("rs_filter", {}) or {}
            rs_ratings = (
                compute_universe_rs_ratings(
                    valid_frames,
                    benchmark_frame,
                    as_of_date=session_date - timedelta(days=1),
                    lookback_weights=rs_filter_cfg.get("lookback_weights"),
                )
                if (benchmark_frame is not None and not benchmark_frame.empty)
                else {}
            )

            # 5. Evaluate Raw Confluence & Per-Guardrail Outlets
            allowed_directions = list(
                self.cfg.get("paper_trading", {}).get("allowed_directions", ["BUY"])
            )
            raw_candidates = []
            filter_drop_counts = {
                "allowed_directions": 0,
                "trend_gate": 0,
                "rsi_exhaustion": 0,
                "wick_filter": 0,
                "adx_filter": 0,
                "market_context": 0,
                "rs_filter": 0,
                "risk_distance": 0,
            }

            for stock in eligible_stocks:
                frame = valid_frames[stock.symbol]
                res, _ = self._scan_stock(stock, frame, provisional=False, diagnostic=True)
                if not res or not res.get("direction"):
                    continue

                direction = res["direction"]
                score = res["confluence_score"]
                total_signals = res["total_signals"]
                checks = res.get("checks", [])
                triggered_inds = [
                    c["name"] for c in checks
                    if c.get("met") and c.get("direction") == direction
                ]
                curr_price = float(frame["close"].iloc[-1])

                # 5.1 allowed_directions
                pass_allowed = direction in allowed_directions
                if not pass_allowed:
                    filter_drop_counts["allowed_directions"] += 1

                # 5.2 trend_gate
                tg_info = res.get("trend_gate") or {"passed": True, "status": "disabled"}
                pass_trend = tg_info.get("passed", True)
                if not pass_trend:
                    filter_drop_counts["trend_gate"] += 1

                # 5.3 rsi_exhaustion
                rsi_info = res.get("rsi_exhaustion") or {"passed": True, "status": "disabled"}
                pass_rsi = rsi_info.get("passed", True)
                if not pass_rsi:
                    filter_drop_counts["rsi_exhaustion"] += 1

                # 5.4 adx_filter
                adx_info = res.get("adx") or {"passed": True, "status": "disabled"}
                pass_adx = adx_info.get("passed", True)
                adx_val = float(adx_info.get("adx") or 0.0)
                if not pass_adx:
                    filter_drop_counts["adx_filter"] += 1

                # 5.5 wick_filter
                wick_info = res.get("wick_filter") or {"passed": True, "status": "disabled"}
                pass_wick = wick_info.get("passed", True)
                clv_val = wick_info.get("clv")
                if not pass_wick:
                    filter_drop_counts["wick_filter"] += 1

                # 5.6 market_context
                mc_mode = m_cfg.get("mode", "informational")
                if mc_mode == "benchmark_filter":
                    pass_regime = buy_regime_allowed if direction == "BUY" else sell_regime_allowed
                else:
                    pass_regime = True
                if not pass_regime:
                    filter_drop_counts["market_context"] += 1

                # 5.7 rs_filter
                stock_rs = rs_ratings.get(stock.symbol)
                rs_enabled = rs_filter_cfg.get("enabled", False)
                min_buy_rs = int(rs_filter_cfg.get("min_rs_rating_buy", 50))
                max_sell_rs = int(rs_filter_cfg.get("max_rs_rating_sell", 50))
                pass_rs = True
                if rs_enabled:
                    if direction == "BUY":
                        pass_rs = (stock_rs is not None and stock_rs >= min_buy_rs)
                    else:
                        pass_rs = (stock_rs is not None and stock_rs <= max_sell_rs)
                if not pass_rs:
                    filter_drop_counts["rs_filter"] += 1

                # 5.8 risk_reference
                risk_cfg = self.cfg.get("risk", {}) or {}
                max_risk_dist = float(risk_cfg.get("max_risk_distance_pct", 8.0))
                risk_ctx = risk_reference(frame, entry_price=curr_price, direction=direction, config=risk_cfg)
                pass_risk = (risk_ctx.get("reason") != "risk_distance_exceeds_max")
                risk_dist_pct = float(risk_ctx.get("risk_distance_pct") or 0.0)
                if not pass_risk:
                    filter_drop_counts["risk_distance"] += 1

                # Final determination
                final_passed = (
                    pass_allowed and pass_trend and pass_rsi and pass_adx
                    and pass_wick and pass_regime and pass_rs and pass_risk
                )

                # Format detailed diagnostic string
                failed_checks = []
                if not pass_allowed:
                    failed_checks.append(f"Direction {direction} not in allowed_directions {allowed_directions}")
                if not pass_trend:
                    failed_checks.append(
                        f"Trend Gate (Price ₹{curr_price:.1f} vs EMA50 ₹{tg_info.get('ema_fast', 0):.1f} vs EMA200 ₹{tg_info.get('ema_slow', 0):.1f})"
                    )
                if not pass_rsi:
                    failed_checks.append(f"RSI Exhaustion ({rsi_info.get('rsi', 0):.1f})")
                if not pass_adx:
                    failed_checks.append(f"ADX Filter (ADX {adx_val:.1f} < {adx_info.get('min_adx', 20.0)} or DI misaligned)")
                if not pass_wick:
                    failed_checks.append(f"Wick Filter (CLV {clv_val})")
                if not pass_regime:
                    failed_checks.append(f"Benchmark Regime Gate (Nifty is {bench_regime})")
                if not pass_rs:
                    failed_checks.append(f"RS Filter (RS {stock_rs} {'<' if direction == 'BUY' else '>'} {min_buy_rs if direction == 'BUY' else max_sell_rs})")
                if not pass_risk:
                    failed_checks.append(f"Risk Distance ({risk_dist_pct:.1f}% > {max_risk_dist:.1f}%)")

                if final_passed:
                    diag_text = (
                        f"✅ PASSED / DISPATCHED: Stop ₹{risk_ctx.get('stop_loss', 0):.2f} | "
                        f"Target ₹{risk_ctx.get('target_price', 0):.2f} | "
                        f"Qty {risk_ctx.get('quantity', 0)} (₹{risk_ctx.get('actual_risk_inr', 0):,.0f} Risk)"
                    )
                else:
                    diag_text = "❌ REJECTED: " + "; ".join(failed_checks)

                raw_candidates.append({
                    "symbol": stock.symbol,
                    "company_name": stock.company_name or stock.symbol,
                    "direction": direction,
                    "confluence_score": score,
                    "total_signals": total_signals,
                    "triggered_indicators": triggered_inds,
                    "close": curr_price,
                    "adx_14": adx_val,
                    "rs_rating": stock_rs,
                    "risk_distance_pct": risk_dist_pct,
                    "final_status": "✅ PASSED / DISPATCHED" if final_passed else "❌ REJECTED",
                    "final_passed": final_passed,
                    "failed_checks": failed_checks,
                    "diagnostic_details": diag_text,
                    "guardrails": {
                        "allowed_directions": {"passed": pass_allowed, "configured": allowed_directions, "value": direction},
                        "trend_gate": {"passed": pass_trend, "info": tg_info},
                        "rsi_exhaustion": {"passed": pass_rsi, "info": rsi_info},
                        "adx_filter": {"passed": pass_adx, "info": adx_info},
                        "wick_filter": {"passed": pass_wick, "info": wick_info},
                        "market_context": {"passed": pass_regime, "regime": bench_regime, "close": bench_close, "sma_20": bench_sma20, "sma_50": bench_sma50},
                        "rs_filter": {"passed": pass_rs, "rs_rating": stock_rs, "min_buy": min_buy_rs, "max_sell": max_sell_rs},
                        "risk_reference": {"passed": pass_risk, "risk_distance_pct": risk_dist_pct, "max_cap": max_risk_dist, "risk_ctx": risk_ctx},
                    },
                })

            # Sort BUY candidates by rs_rating descending
            buy_candidates = sorted(
                [c for c in raw_candidates if c["direction"] == "BUY"],
                key=lambda x: (-(x["rs_rating"] if x["rs_rating"] is not None else -1), -x["confluence_score"], x["symbol"]),
            )
            sell_candidates = sorted(
                [c for c in raw_candidates if c["direction"] == "SELL"],
                key=lambda x: (-(x["rs_rating"] if x["rs_rating"] is not None else -1), -x["confluence_score"], x["symbol"]),
            )

            # 6. Database Live Executions & Integrity Check
            signals_db = [
                {
                    "symbol": s.symbol,
                    "signal_type": s.signal_type,
                    "direction": s.direction,
                    "price": s.price,
                    "confluence_score": int(s.indicator_value or 0),
                    "created_at": s.created_at.isoformat() if s.created_at else None,
                }
                for s in db.query(Signal).filter_by(signal_date=session_date).all()
            ]
            paper_trades_db = [
                {
                    "symbol": pt.symbol,
                    "direction": pt.direction,
                    "entry_price": pt.entry_price,
                    "alert_type": pt.alert_type,
                    "alert_tier": pt.alert_tier,
                    "status": pt.status,
                    "confluence_score": pt.confluence_score,
                    "rs_rating_at_entry": pt.rs_rating_at_entry,
                }
                for pt in db.query(PaperTrade).filter_by(entry_date=session_date).all()
            ]
            open_paper_trades = [
                {
                    "symbol": pt.symbol,
                    "direction": pt.direction,
                    "entry_date": pt.entry_date.isoformat(),
                    "entry_price": pt.entry_price,
                    "alert_type": pt.alert_type,
                }
                for pt in db.query(PaperTrade).filter_by(status="open").all()
            ]
            sqlite_integrity = str(db.execute(text("PRAGMA quick_check")).scalar() or "ok")

            # 7. Ablation States Comparison
            pre_phase_3a_passed = []
            for c in raw_candidates:
                g = c["guardrails"]
                # In Pre-Phase-3A: stop_atr_multiple was 1.5 (vs 2.0 in Phase 3A), max_risk_distance_pct was 6.0%
                pre_3a_dist = c["risk_distance_pct"] * (1.5 / 2.0)
                if (
                    g["trend_gate"]["passed"]
                    and g["rsi_exhaustion"]["passed"]
                    and g["wick_filter"]["passed"]
                    and g["adx_filter"]["passed"]
                    and pre_3a_dist <= 6.0
                ):
                    pre_phase_3a_passed.append(c["symbol"])

            locked_prod_passed = [c["symbol"] for c in raw_candidates if c["final_passed"]]

            ablation = {
                "raw_confluence_count": len(raw_candidates),
                "pre_phase_3a_count": len(pre_phase_3a_passed),
                "pre_phase_3a_symbols": sorted(pre_phase_3a_passed),
                "locked_prod_count": len(locked_prod_passed),
                "locked_prod_symbols": sorted(locked_prod_passed),
            }

            # 8. Strategy Config Hash
            strategy_cfg = {
                key: self.cfg.get(key)
                for key in ("signals", "confluence", "guardrails", "backtest", "risk", "market_context")
            }
            config_hash = hashlib.sha256(json.dumps(strategy_cfg, sort_keys=True, default=str).encode()).hexdigest()

            return {
                "session_date": session_date.isoformat(),
                "strategy_version": str(self.cfg.get("paper_trading", {}).get("strategy_version", "v2_p3_5_bear_rs50")),
                "config_hash_sha256": config_hash,
                "universe_size": universe_size,
                "eligible_count": eligible_count,
                "evaluated_count": evaluated_count,
                "coverage_ratio": coverage_ratio,
                "coverage_percent": coverage_percent,
                "coverage_healthy": coverage_healthy,
                "failed_symbols": failed_symbols,
                "market_context": {
                    "benchmark_key": benchmark_key,
                    "benchmark_close": bench_close,
                    "sma_20": bench_sma20,
                    "sma_50": bench_sma50,
                    "regime": bench_regime,
                    "allowed_buy_regimes": allowed_buy_regimes,
                    "allowed_sell_regimes": allowed_sell_regimes,
                    "buy_allowed": buy_regime_allowed,
                    "sell_allowed": sell_regime_allowed,
                },
                "raw_candidates": raw_candidates,
                "buy_candidates": buy_candidates,
                "sell_candidates": sell_candidates,
                "filter_drop_counts": filter_drop_counts,
                "dispatched_signals_db": signals_db,
                "paper_trades_db": paper_trades_db,
                "open_paper_trades": open_paper_trades,
                "sqlite_integrity": sqlite_integrity,
                "ablation": ablation,
            }
        finally:
            db.close()
