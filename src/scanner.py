"""
Scanner — orchestrates signal detection across all NSE/BSE stocks.

Flow per scan cycle:
1. Get list of all active stocks from DB (refreshed daily from Upstox instruments)
2. For each stock (batched):
   a. Fetch daily OHLCV history (250 days) from Upstox
   b. Run all enabled signal detectors
   c. For any triggered signal:
      - Fetch fundamental data (from cache or Screener.in)
      - Generate chart
      - Save signal to DB
      - Queue for notification
3. Dispatch queued signals to all enabled notifiers

Performance notes:
- 5000+ stocks fetched in parallel (thread pool, respect API rate limits)
- Chart generation is lazy (only on triggered signals)
- Fundamentals fetched from SQLite cache (refreshed every 24h)
- Deduplication: one alert per stock per signal_type per trading day
"""
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from typing import Optional

import pandas as pd

from src.config_loader import get_config
from src.db import (
    Stock,
    get_session,
    mark_signal_notified,
    prune_ohlcv_cache,
    upsert_signal,
)
from src.fetcher.fundamentals import FundamentalsFetcher
from src.fetcher.upstox import UpstoxFetcher
from src.notifiers.base import AlertPayload, BaseNotifier
from src.signals.ema_cross import detect_all_ema_crossovers
from src.signals.macd import detect_macd_signal
from src.signals.rsi import detect_rsi_signal
from src.signals.volume_breakout import detect_volume_breakout

logger = logging.getLogger(__name__)


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
    """Main scanning engine."""

    def __init__(self):
        self.cfg = get_config()
        self.fetcher = UpstoxFetcher()
        self.fundamentals = FundamentalsFetcher()
        self.notifiers = _build_notifiers(self.cfg)
        self.signal_cfg = self.cfg.get("signals", {})
        self.universe_cfg = self.cfg.get("universe", {})

    # ─── Stock Universe ──────────────────────────────────────────────────

    def refresh_instrument_master(self) -> int:
        """Download fresh instrument list from Upstox and upsert into DB."""
        logger.info("Refreshing instrument master from Upstox...")
        session = get_session()
        try:
            count = 0
            for exchange in ["NSE"]:  # Add "BSE" if needed
                instruments = self.fetcher.get_all_eq_symbols(exchange)
                for inst in instruments:
                    existing = session.query(Stock).filter_by(
                        symbol=inst["symbol"], exchange=exchange
                    ).first()
                    if existing:
                        existing.instrument_key = inst["instrument_key"]
                        existing.isin = inst.get("isin", "")
                        existing.company_name = inst.get("company_name", "")
                        existing.last_updated = datetime.utcnow()
                    else:
                        session.add(Stock(
                            symbol=inst["symbol"],
                            isin=inst.get("isin", ""),
                            company_name=inst.get("company_name", ""),
                            exchange=exchange,
                            instrument_key=inst["instrument_key"],
                            series=inst.get("series", "EQ"),
                            is_active=True,
                            last_updated=datetime.utcnow(),
                        ))
                    count += 1
                if count % 500 == 0:
                    session.commit()
            session.commit()
            logger.info(f"Instrument master refreshed: {count} stocks.")
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
            # Detach from session (avoid lazy loading issues in threads)
            session.expunge_all()
            return stocks
        finally:
            session.close()

    # ─── Single Stock Scan ───────────────────────────────────────────────

    def _scan_stock(self, stock: Stock) -> tuple[list[dict], Optional[pd.DataFrame]]:
        """
        Scan a single stock for all enabled signals.
        Returns (list of triggered signal dicts, df of daily candles).
        """
        triggered = []
        try:
            # Fetch 250 days of daily candles
            df = self.fetcher.get_daily_candles(stock.instrument_key, days=250)
            if df.empty or len(df) < 30:
                return [], None

            price_floor = self.universe_cfg.get("min_price", 10)
            if df["close"].iloc[-1] < price_floor:
                return [], None  # Skip penny stocks

            # ── RSI ─────────────────────────────────────────────────────
            if self.signal_cfg.get("rsi", {}).get("enabled", True):
                rsi_cfg = self.signal_cfg["rsi"]
                rsi_result = detect_rsi_signal(
                    stock.symbol, df,
                    period=rsi_cfg.get("period", 14),
                    oversold=rsi_cfg.get("oversold_threshold", 30),
                    overbought=rsi_cfg.get("overbought_threshold", 70),
                )
                if rsi_result.triggered:
                    triggered.append({
                        "type": rsi_result.signal_type,
                        "direction": rsi_result.direction,
                        "price": rsi_result.price,
                        "indicator_value": rsi_result.rsi_value,
                        "detail": {"rsi": rsi_result.rsi_value},
                    })

            # ── MACD ────────────────────────────────────────────────────
            if self.signal_cfg.get("macd", {}).get("enabled", True):
                macd_cfg = self.signal_cfg["macd"]
                macd_result = detect_macd_signal(
                    stock.symbol, df,
                    fast=macd_cfg.get("fast_period", 12),
                    slow=macd_cfg.get("slow_period", 26),
                    signal_period=macd_cfg.get("signal_period", 9),
                )
                if macd_result.triggered:
                    triggered.append({
                        "type": macd_result.signal_type,
                        "direction": macd_result.direction,
                        "price": macd_result.price,
                        "indicator_value": macd_result.macd_value,
                        "detail": {
                            "macd": macd_result.macd_value,
                            "signal": macd_result.signal_value,
                            "histogram": macd_result.histogram,
                        },
                    })

            # ── EMA Crossovers ──────────────────────────────────────────
            if self.signal_cfg.get("ema_crossover", {}).get("enabled", True):
                ema_cfg = self.signal_cfg["ema_crossover"]
                pairs = [tuple(p) for p in ema_cfg.get("pairs", [[9, 21], [21, 50], [50, 200]])]
                ema_results = detect_all_ema_crossovers(stock.symbol, df, pairs=pairs)
                for r in ema_results:
                    triggered.append({
                        "type": r.signal_type,
                        "direction": r.direction,
                        "price": r.price,
                        "indicator_value": r.fast_ema_value,
                        "detail": {
                            f"ema_{r.fast_period}": r.fast_ema_value,
                            f"ema_{r.slow_period}": r.slow_ema_value,
                        },
                    })

            # ── Volume Breakout ─────────────────────────────────────────
            if self.signal_cfg.get("volume_breakout", {}).get("enabled", True):
                vol_cfg = self.signal_cfg["volume_breakout"]
                vol_result = detect_volume_breakout(
                    stock.symbol, df,
                    volume_multiplier=vol_cfg.get("volume_multiplier", 2.0),
                    lookback_days=vol_cfg.get("breakout_lookback_days", 20),
                )
                if vol_result.triggered:
                    triggered.append({
                        "type": vol_result.signal_type,
                        "direction": vol_result.direction,
                        "price": vol_result.current_price,
                        "indicator_value": vol_result.volume_ratio,
                        "detail": {
                            "volume_ratio": vol_result.volume_ratio,
                            "current_volume": vol_result.current_volume,
                            "avg_volume": vol_result.avg_volume,
                            "breakout_level": vol_result.breakout_level,
                        },
                    })

            return triggered, df

        except Exception as e:
            logger.error(f"Error scanning {stock.symbol}: {e}", exc_info=False)
            return [], None

    # ─── Full Scan Cycle ─────────────────────────────────────────────────

    def run_scan(self, limit: Optional[int] = None) -> int:
        """
        Run a full scan across active stocks.
        Returns number of new signals detected.
        """
        logger.info("Starting market scan...")
        stocks = self.get_active_stocks(limit=limit)
        if not stocks:
            logger.warning("No stocks in database. Run refresh_instrument_master() first.")
            return 0

        today = date.today()
        new_signal_count = 0
        db = get_session()

        # Scan stocks in parallel (thread pool, Upstox rate limit aware)
        max_workers = 10  # Conservative to respect Upstox rate limits
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(self._scan_stock, stock): stock for stock in stocks}

            for future in as_completed(futures):
                stock = futures[future]
                try:
                    signals, df = future.result()
                except Exception as e:
                    logger.error(f"Future error for {stock.symbol}: {e}")
                    signals, df = [], None

                if not signals:
                    continue

                # Process each triggered signal
                for sig in signals:
                    try:
                        # Fetch fundamentals (cached)
                        fund = self.fundamentals.get_fundamentals(stock.symbol, stock.isin or "")

                        # Generate chart
                        chart_path = None
                        if df is not None and not df.empty:
                            from charts.chart_generator import generate_signal_chart
                            chart_path = generate_signal_chart(
                                symbol=stock.symbol,
                                company_name=stock.company_name or stock.symbol,
                                df=df,
                                signal_type=sig["type"],
                                direction=sig["direction"],
                                indicator_value=sig.get("indicator_value"),
                            )

                        # Save signal to DB (deduplicated)
                        saved = upsert_signal(
                            session=db,
                            symbol=stock.symbol,
                            signal_date=today,
                            signal_type=sig["type"],
                            direction=sig["direction"],
                            price=sig["price"],
                            indicator_value=sig.get("indicator_value", 0),
                            indicator_detail=json.dumps(sig.get("detail", {})),
                            chart_path=chart_path,
                        )

                        # Build alert payload and notify
                        payload = AlertPayload(
                            symbol=stock.symbol,
                            company_name=stock.company_name or stock.symbol,
                            signal_type=sig["type"],
                            direction=sig["direction"],
                            price=sig["price"],
                            indicator_value=sig.get("indicator_value"),
                            indicator_detail=sig.get("detail", {}),
                            chart_path=chart_path,
                            mcap_cr=fund.get("mcap_cr") if fund else None,
                            cap_category=fund.get("cap_category") if fund else None,
                            sector=fund.get("sector") if fund else None,
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

                        self._dispatch_alert(payload)
                        mark_signal_notified(db, saved.id)
                        new_signal_count += 1

                    except Exception as e:
                        logger.error(f"Error processing signal for {stock.symbol}: {e}", exc_info=True)

        db.close()
        logger.info(f"Scan complete. {new_signal_count} new signals detected and dispatched.")
        return new_signal_count

    def _dispatch_alert(self, payload: AlertPayload) -> None:
        """Send alert to all enabled notifiers."""
        for notifier in self.notifiers:
            try:
                notifier.notify(payload, send_chart=True)
            except Exception as e:
                logger.error(f"Notifier {type(notifier).__name__} failed: {e}")
