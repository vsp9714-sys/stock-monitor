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
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
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
from src.signals.ema_cross import detect_ema_crossover
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
        """Download fresh instrument list from Upstox and upsert into DB."""
        logger.info("Refreshing instrument master from Upstox...")
        session = get_session()
        try:
            count = 0
            for exchange in ["NSE"]:
                instruments = self.fetcher.get_all_eq_symbols(exchange)
                for inst in instruments:
                    existing = session.query(Stock).filter_by(
                        symbol=inst["symbol"], exchange=exchange
                    ).first()
                    now = datetime.now(timezone.utc)
                    if existing:
                        existing.instrument_key = inst["instrument_key"]
                        existing.isin = inst.get("isin", "")
                        existing.company_name = inst.get("company_name", "")
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
            session.expunge_all()
            return stocks
        finally:
            session.close()

    # ─── Single Stock Scan & Confluence Evaluation ────────────────────────

    def _scan_stock(self, stock: Stock) -> tuple[Optional[dict], Optional[pd.DataFrame]]:
        """
        Scan a single stock across all indicator checks and evaluate confluence.
        Returns (confluence_dict or None, df of daily candles).
        """
        try:
            # Fetch 250 days of daily candles
            df = self.fetcher.get_daily_candles(stock.instrument_key, days=250)
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
            return None, None

    # ─── Full Scan Cycle ─────────────────────────────────────────────────

    def run_scan(self, limit: Optional[int] = None) -> int:
        """
        Run a full scan across active stocks with confluence filtering.
        Returns number of confluence alerts dispatched.
        """
        logger.info("Starting market scan with confluence filtering...")
        stocks = self.get_active_stocks(limit=limit)
        if not stocks:
            logger.warning("No stocks in database. Run refresh_instrument_master() first.")
            return 0

        today = date.today()
        new_signal_count = 0
        db = get_session()

        max_workers = 10  # Conservative Upstox rate limit concurrency
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(self._scan_stock, stock): stock for stock in stocks}

            for future in as_completed(futures):
                stock = futures[future]
                try:
                    res, df = future.result()
                except Exception as e:
                    logger.error(f"Future error for {stock.symbol}: {e}")
                    res, df = None, None

                if not res:
                    continue

                try:
                    # Fetch fundamentals (cached)
                    fund = self.fundamentals.get_fundamentals(stock.symbol, stock.isin or "")

                    # Generate multi-panel chart
                    chart_path = None
                    if df is not None and not df.empty:
                        from charts.chart_generator import generate_signal_chart
                        chart_path = generate_signal_chart(
                            symbol=stock.symbol,
                            company_name=stock.company_name or stock.symbol,
                            df=df,
                            signal_type=res["signal_type"],
                            direction=res["direction"],
                            indicator_value=float(res["confluence_score"]),
                        )

                    # Save consolidated signal to DB (deduplicated per symbol + date + signal_type)
                    saved = upsert_signal(
                        session=db,
                        symbol=stock.symbol,
                        signal_date=today,
                        signal_type=res["signal_type"],
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

                    # Build multi-signal alert payload
                    payload = AlertPayload(
                        symbol=stock.symbol,
                        company_name=stock.company_name or stock.symbol,
                        signal_type=res["signal_type"],
                        direction=res["direction"],
                        price=res["price"],
                        indicator_value=float(res["confluence_score"]),
                        indicator_detail={
                            "confluence_score": res["confluence_score"],
                            "checks": res["checks"],
                        },
                        confluence_score=res["confluence_score"],
                        total_signals=res["total_signals"],
                        checks=res["checks"],
                        chart_path=chart_path,
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

                    # Dispatch single consolidated alert
                    self._dispatch_alert(payload)
                    mark_signal_notified(db, saved.id)
                    new_signal_count += 1
                    logger.info(
                        f"Confluence alert dispatched for {stock.symbol}: "
                        f"{res['direction']} ({res['confluence_score']}/{res['total_signals']} signals met)"
                    )

                except Exception as e:
                    logger.error(f"Error processing confluence alert for {stock.symbol}: {e}", exc_info=True)

        db.close()
        logger.info(f"Scan complete. {new_signal_count} confluence alerts detected and dispatched.")
        return new_signal_count

    def _dispatch_alert(self, payload: AlertPayload) -> None:
        """Send alert to all enabled notifiers."""
        for notifier in self.notifiers:
            try:
                notifier.notify(payload, send_chart=True)
            except Exception as e:
                logger.error(f"Notifier {type(notifier).__name__} failed: {e}")
