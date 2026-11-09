"""
One-shot sweep to process and dispatch all signals that were triggered during
the 2026-11-09 session but failed to serialize due to the NumPy boolean bug (#25).
"""
import json
import logging
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
import pandas as pd

from src.db import (
    Signal,
    get_engine,
    get_session,
    upsert_signal,
    seed_legacy_deliveries,
)
from src.scanner import Scanner, _json_default, _daily_timestamp
from src.notifiers.base import AlertPayload
from src.risk import risk_reference

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("sweep_missed_alerts")

IST = ZoneInfo("Asia/Kolkata")


def sweep_today():
    get_engine()
    scanner = Scanner()
    db = get_session()
    today = date(2026, 11, 9)

    stocks = scanner.get_active_stocks()
    logger.info("Loaded %d active stocks for universe %s", len(stocks), scanner.universe_cfg.get("mode"))

    # Fetch live quotes
    instrument_keys = [s.instrument_key for s in stocks]
    quotes = scanner.fetcher.get_full_market_quotes(
        instrument_keys,
        batch_size=int(scanner.cfg.get("upstox", {}).get("quote_batch_size", 100)),
        allow_missing=True,
    )
    logger.info("Retrieved %d quotes from Upstox", len(quotes))

    # Load cached history
    frames = scanner._load_cached_daily_frames(stocks, db)

    # Seed delivery destinations
    destinations = [
        target
        for notifier in scanner.notifiers
        for target in notifier.delivery_targets()
    ]
    seed_legacy_deliveries(db, destinations)

    signals_dispatched = 0
    signals_found = []

    for stock in stocks:
        frame = frames.get(stock.symbol)
        if frame is None or frame.empty or len(frame) < 30:
            continue

        quote = quotes.get(stock.instrument_key)
        if not quote:
            continue

        # Completed history before today
        dates = pd.to_datetime(frame["ts"]).map(lambda v: _daily_timestamp(v).date())
        completed = frame.loc[dates < today].reset_index(drop=True)
        if len(completed) < 30:
            continue

        # Paths to evaluate:
        # 1. Confirmed daily signal on completed candles
        # 2. Provisional intraday signal with today's quote bar
        candidates = []
        daily_res, _ = scanner._scan_stock(stock, completed, provisional=False)
        if daily_res:
            candidates.append((daily_res, completed, False))

        quote_bar = {
            "ts": pd.Timestamp(today),
            **{f: float(quote[f]) for f in ("open", "high", "low", "close", "volume")},
        }
        intraday = pd.concat([completed, pd.DataFrame([quote_bar])], ignore_index=True)
        provisional_res, _ = scanner._scan_stock(stock, intraday, provisional=True)
        if provisional_res:
            provisional_res = {
                **provisional_res,
                "session_date": today.isoformat(),
                "quote_timestamp": datetime.fromtimestamp(
                    int(quote["last_trade_time"]) / 1000, timezone.utc
                ).isoformat(),
                "bar_status": "provisional",
            }
            candidates.append((provisional_res, intraday, True))

        for res, df, provisional in candidates:
            try:
                fund = scanner.fundamentals.get_fundamentals(stock.symbol, stock.isin or "")
                signal_type = res["signal_type"]
                quote_price = float(quote["last_price"])
                res = {**res, "price": quote_price}

                risk_ctx = risk_reference(
                    completed, entry_price=quote_price,
                    direction=res["direction"], config=scanner.cfg.get("risk", {}),
                )

                # Generate chart
                from charts.chart_generator import generate_signal_chart
                chart_path = generate_signal_chart(
                    symbol=stock.symbol,
                    company_name=stock.company_name or stock.symbol,
                    df=df,
                    signal_type=signal_type,
                    direction=res["direction"],
                    indicator_value=float(res["confluence_score"]),
                )

                # Persist with safe JSON serialization
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
                        "confluence_mode": res.get("confluence_mode", "crossover"),
                        "score_version": res.get("score_version", 1),
                        "checks": res["checks"],
                        "risk_context": risk_ctx,
                        "fundamental_provenance": {
                            key: fund.get(key) for key in (
                                "source_url", "fetched_at", "cache_age_hours", "freshness",
                                "data_basis", "quarterly_data_status"
                            )
                        } if fund else {"freshness": "unavailable"},
                    }, default=_json_default),
                    chart_path=chart_path,
                )

                saved_detail = json.loads(saved.indicator_detail or "{}")
                payload = AlertPayload(
                    symbol=stock.symbol,
                    company_name=stock.company_name or stock.symbol,
                    signal_type=signal_type,
                    direction=res["direction"],
                    price=res["price"],
                    price_source="Upstox last_price",
                    quote_timestamp=datetime.fromtimestamp(
                        int(quote["last_trade_time"]) / 1000, timezone.utc
                    ).isoformat() if quote.get("last_trade_time") else None,
                    indicator_value=float(res["confluence_score"]),
                    indicator_detail=saved_detail,
                    confluence_score=int(res["confluence_score"]),
                    total_signals=int(res["total_signals"]),
                    checks=saved_detail.get("checks", res["checks"]),
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
                    risk_context=risk_ctx,
                )

                dispatched = scanner._dispatch_alert(payload, db, saved.id)
                if dispatched:
                    signals_dispatched += 1
                    signals_found.append(f"{stock.symbol} ({signal_type}: {res['direction']} score={res['confluence_score']})")
                    logger.info("SUCCESS: Alert dispatched for %s [%s]", stock.symbol, signal_type)
            except Exception as e:
                logger.error("Failed to process/dispatch alert for %s: %s", stock.symbol, e, exc_info=True)

    db.close()
    logger.info("Sweep complete! Total signals dispatched to Telegram: %d", signals_dispatched)
    for s in signals_found:
        logger.info("  * %s", s)
    return signals_dispatched


if __name__ == "__main__":
    sweep_today()
