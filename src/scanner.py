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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import func

from src.config_loader import get_config
from src.db import (
    DailyCandleRefresh,
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
from src.signals.volume_breakout import detect_volume_breakout

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True)
class ScanSummary:
    universe_size: int
    evaluated_count: int
    failed_count: int
    alerts_dispatched: int
    complete: bool


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
        """Get all active stocks from the DB."""
        session = get_session()
        try:
            mode = self.universe_cfg.get("mode", "all_nse")
            custom_symbols = self.universe_cfg.get("custom_symbols", [])

            query = session.query(Stock).filter_by(is_active=True)
            if mode == "custom" and custom_symbols:
                query = query.filter(Stock.symbol.in_(custom_symbols))

            if limit:
                query = query.limit(limit)

            stocks = query.all()
            session.expunge_all()
            return stocks
        finally:
            session.close()

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
                    if isinstance(exc, UpstoxAPIError):
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
        if current.weekday() >= 5 or not (datetime_time(6, 0) <= current.time() < datetime_time(7, 0)):
            logger.info("Morning daily-candle readiness check skipped outside 06:00-07:00 IST weekday window.")
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

    def _scan_stock(self, stock: Stock, df: pd.DataFrame) -> tuple[Optional[dict], pd.DataFrame]:
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
                )
                rsi_val = rsi_result.rsi_value
                if rsi_result.triggered:
                    checks.append({
                        "name": "RSI (14)",
                        "signal_type": "rsi",
                        "met": True,
                        "direction": rsi_result.direction,
                        "value_text": f"RSI {rsi_val:.1f}",
                        "description": f"Oversold (≤ {oversold})" if rsi_result.direction == "BUY" else f"Overbought (≥ {overbought})",
                    })
                else:
                    checks.append({
                        "name": "RSI (14)",
                        "signal_type": "rsi",
                        "met": False,
                        "direction": None,
                        "value_text": f"RSI {rsi_val:.1f}",
                        "description": "Neutral (30–70)",
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
                        "met": True,
                        "direction": macd_result.direction,
                        "value_text": f"MACD {macd_result.macd_value:.2f}",
                        "description": "Bullish Crossover" if macd_result.direction == "BUY" else "Bearish Crossover",
                    })
                else:
                    checks.append({
                        "name": "MACD (12,26,9)",
                        "signal_type": "macd",
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
                            "met": True,
                            "direction": ema_res.direction,
                            "value_text": f"₹{ema_res.fast_ema_value:.1f} vs ₹{ema_res.slow_ema_value:.1f}",
                            "description": f"{ema_res.direction.title()} Cross",
                        })
                    else:
                        checks.append({
                            "name": chk_name,
                            "signal_type": f"ema_{fast_p}x{slow_p}",
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
                            "met": True,
                            "direction": vol_result.direction,
                            "value_text": f"{vol_ratio:.1f}× Avg Vol",
                            "description": "Bullish Breakout" if vol_result.direction == "BUY" else "Bearish Breakdown",
                        })
                    else:
                        checks.append({
                            "name": "Volume Breakout",
                            "signal_type": "volume_breakout",
                            "met": False,
                            "direction": None,
                            "value_text": f"{vol_ratio:.1f}× Avg Vol",
                            "description": "Spike (No Price Breakout)",
                        })
                else:
                    checks.append({
                        "name": "Volume Breakout",
                        "signal_type": "volume_breakout",
                        "met": False,
                        "direction": None,
                        "value_text": f"{vol_ratio:.1f}× Avg Vol",
                        "description": "Normal Volume",
                    })

            # ── Evaluate Confluence ─────────────────────────────────────
            buy_met = [c for c in checks if c["met"] and c["direction"] == "BUY"]
            sell_met = [c for c in checks if c["met"] and c["direction"] == "SELL"]

            min_signals = self.confluence_cfg.get("min_signals_to_alert", 2)
            direction_consensus = self.confluence_cfg.get("direction_consensus", True)

            if len(buy_met) > len(sell_met):
                consensus_dir = "BUY"
                met_count = len(buy_met)
                conflicting_count = len(sell_met)
            elif len(sell_met) > len(buy_met):
                consensus_dir = "SELL"
                met_count = len(sell_met)
                conflicting_count = len(buy_met)
            else:
                consensus_dir = None
                met_count = 0
                conflicting_count = 0

            # Discard if no consensus direction
            if consensus_dir is None:
                return None, df

            # Discard if there are conflicting BUY vs SELL signals
            if direction_consensus and conflicting_count > 0:
                logger.debug(
                    f"{stock.symbol}: Skipping due to conflicting signals "
                    f"({len(buy_met)} BUY vs {len(sell_met)} SELL)"
                )
                return None, df

            # Check if confluence threshold is reached
            if met_count < min_signals:
                return None, df

            # Confluence criteria satisfied!
            result = {
                "symbol": stock.symbol,
                "company_name": stock.company_name or stock.symbol,
                "direction": consensus_dir,
                "confluence_score": met_count,
                "total_signals": len(checks),
                "price": current_price,
                "checks": checks,
                "signal_type": f"confluence_{consensus_dir.lower()}",
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

    def run_scan(self, limit: Optional[int] = None) -> ScanSummary:
        """Scan cached history plus batched live OHLCV and report coverage gaps."""
        logger.info("Starting market scan with cached history and batched quotes...")
        stocks = self.get_active_stocks(limit=limit)
        if not stocks:
            logger.warning("No stocks in database. Run refresh_instrument_master() first.")
            return ScanSummary(0, 0, 0, 0, True)

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
                return ScanSummary(len(stocks), 0, len(stocks), 0, False)

            quote_dates = set()
            missing_quote_stocks = []
            fresh_quotes = {}
            now = datetime.now(timezone.utc)
            max_age = int(self.cfg.get("upstox", {}).get("max_quote_age_seconds", 300))
            for stock in stocks:
                quote = quotes.get(stock.instrument_key)
                if quote is None:
                    missing_quote_stocks.append(stock)
                    continue
                try:
                    trade_time = datetime.fromtimestamp(quote["last_trade_time"] / 1000, timezone.utc)
                    ltp = float(quote["last_price"])
                    age = (now - trade_time).total_seconds()
                    if ltp <= 0 or age < -30 or age > max_age:
                        raise ValueError("stale or invalid LTP")
                except (KeyError, TypeError, ValueError, OverflowError, OSError):
                    logger.warning("Skipping %s: missing, invalid, or stale Upstox last trade", stock.symbol)
                    missing_quote_stocks.append(stock)
                    continue
                quote_dates.add(trade_time.astimezone(IST).date())
                fresh_quotes[stock.instrument_key] = quote
            if missing_quote_stocks:
                logger.warning(
                    "Quote response omitted %s/%s symbols; skipping only those symbols: %s",
                    len(missing_quote_stocks), len(stocks),
                    ", ".join(stock.symbol for stock in missing_quote_stocks[:25]),
                )
            if not quote_dates:
                logger.error("Scan incomplete: quote response contained no usable OHLCV; no alerts sent.")
                return ScanSummary(len(stocks), 0, len(stocks), 0, False)
            if len(quote_dates) != 1:
                logger.error("Scan incomplete: quote batches returned mixed trading dates; no alerts sent.")
                return ScanSummary(len(stocks), 0, len(stocks), 0, False)
            # Quote OHLC is a live session snapshot: never append it to canonical
            # daily candles or use it as a finalized input to swing indicators.
            eligible_stocks = [
                stock for stock in stocks
                if stock.instrument_key in fresh_quotes and len(frames[stock.symbol]) >= minimum
            ]
            insufficient_count = sum(
                stock.instrument_key in fresh_quotes and len(frames[stock.symbol]) < minimum
                for stock in stocks
            )
            unavailable_count = len(missing_quote_stocks)
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
            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = {
                    executor.submit(self._scan_stock, stock, frames[stock.symbol]): stock
                    for stock in eligible_stocks
                }
                for future in as_completed(futures):
                    stock = futures[future]
                    try:
                        result, frame = future.result()
                        evaluations.append((stock, result, frame))
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
                    unavailable_count + insufficient_count + len(failed), 0, False,
                )

            # Alert deduplication follows the market-data session, not the host
            # calendar date (important for manual scans after the market closes).
            today = next(iter(quote_dates))
            new_signal_count = 0
            destinations = [
                target for notifier in self.notifiers for target in notifier.delivery_targets()
            ]
            seed_legacy_deliveries(db, destinations)

            for stock, res, df in evaluations:
                if not res:
                    continue
                try:
                    fund = self.fundamentals.get_fundamentals(stock.symbol, stock.isin or "")
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
                            "confluence_score": res["confluence_score"],
                            "total_signals": res["total_signals"],
                            "checks": res["checks"],
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
                        confluence_score=saved_score,
                        total_signals=saved_total,
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
                    )
                    if self._dispatch_alert(payload, db, saved.id):
                        new_signal_count += 1
                        logger.info(
                            "Confluence alert/update dispatched for %s: %s (%s/%s checks)",
                            stock.symbol, res["direction"], saved_score, saved_total,
                        )
                except Exception as exc:
                    logger.error("Error processing alert for %s: %s", stock.symbol, exc, exc_info=True)

            logger.info(
                "Scan complete: evaluated %s/%s symbols; %s alerts dispatched.",
                len(eligible_stocks), len(stocks), new_signal_count,
            )
            return ScanSummary(
                len(stocks), len(eligible_stocks), unavailable_count + insufficient_count,
                new_signal_count, unavailable_count == 0 and insufficient_count == 0,
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
