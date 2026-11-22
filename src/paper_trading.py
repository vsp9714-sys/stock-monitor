"""
Paper Trading & Validation Framework.

Empirically validates signal quality by:
1. Creating paper trade entries when alerts fire (with deduplication & risk references)
2. Monitoring open trades during scan cycles (updating MFE/MAE, stop/target/time exits)
3. Firing milestone alerts at ±2.5%, ±5.0%
4. Compiling and broadcasting weekly performance reports (win rate, R-multiple, tier breakdown)
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from src.db import PaperTrade, PaperTradeMilestone, PaperTradeWeeklyReport, get_session
from src.notifiers.base import BaseNotifier

logger = logging.getLogger(__name__)


def record_paper_trade_entry(
    session: Session,
    signal_id: Optional[int],
    symbol: str,
    direction: str,
    entry_price: float,
    risk_ref: dict[str, Any],
    confluence_result: dict[str, Any],
    alert_tier: str,
    session_date: date,
    config: dict[str, Any],
) -> Optional[PaperTrade]:
    """
    Auto-create a paper trade entry when a confluence alert fires.
    Applies deduplication rules:
    - Unique constraint: (symbol, direction, entry_date)
    - Open trade guard: cannot enter if an open trade for symbol & direction exists
    - Cooldown window: cannot re-enter within cooldown_days
    """
    pt_cfg = config.get("paper_trading", {})
    if not pt_cfg.get("enabled", True):
        return None

    try:
        # Dedup Rule 1: No duplicate trade on same session date
        existing_today = session.query(PaperTrade).filter_by(
            symbol=symbol, direction=direction, entry_date=session_date
        ).first()
        if existing_today:
            logger.debug("Paper trade already exists today for %s %s", symbol, direction)
            return existing_today

        # Dedup Rule 2: No new trade if active open trade exists in same direction
        open_trade = session.query(PaperTrade).filter_by(
            symbol=symbol, direction=direction, status="open"
        ).first()
        if open_trade:
            logger.debug("Active open paper trade already exists for %s %s; skipping duplicate", symbol, direction)
            return None

        # Dedup Rule 3: Cooldown period (minimum days between trades on same symbol & direction)
        cooldown_days = int(pt_cfg.get("cooldown_days", 3))
        if cooldown_days > 0:
            recent_trade = session.query(PaperTrade).filter(
                PaperTrade.symbol == symbol,
                PaperTrade.direction == direction,
                PaperTrade.entry_date >= session_date - timedelta(days=cooldown_days),
            ).first()
            if recent_trade:
                logger.debug(
                    "Paper trade for %s %s suppressed by %d-day cooldown (last entered %s)",
                    symbol, direction, cooldown_days, recent_trade.entry_date,
                )
                return None

        # Determine stop loss and target price
        if risk_ref and risk_ref.get("status") == "available":
            stop_loss = float(risk_ref["stop_reference"])
            target_price = float(risk_ref["target_reference"])
            atr = float(risk_ref.get("atr") or 0.0)
            risk_dist = float(risk_ref.get("risk_distance") or 0.0)
        else:
            # Fallback risk reference: 2% stop, 4% target (2.0R)
            risk_dist = entry_price * 0.02
            stop_loss = entry_price - risk_dist if direction == "BUY" else entry_price + risk_dist
            target_price = entry_price + (risk_dist * 2.0) if direction == "BUY" else entry_price - (risk_dist * 2.0)
            atr = None

        confluence_score = int(
            confluence_result.get("confluence_score", confluence_result.get("score", 0))
        )
        group_votes = confluence_result.get("group_votes", {})

        trade = PaperTrade(
            signal_id=signal_id,
            symbol=symbol,
            direction=direction,
            entry_price=round(float(entry_price), 2),
            entry_date=session_date,
            entry_datetime=datetime.now(timezone.utc),
            stop_loss=round(stop_loss, 2),
            target_price=round(target_price, 2),
            atr_at_entry=round(atr, 2) if atr is not None else None,
            risk_distance=round(risk_dist, 2) if risk_dist is not None else None,
            confluence_score=confluence_score,
            alert_tier=alert_tier,
            signal_groups_json=json.dumps(group_votes),
            max_favorable_excursion=round(float(entry_price), 2),
            max_adverse_excursion=round(float(entry_price), 2),
            status="open",
            created_at=datetime.now(timezone.utc),
        )
        session.add(trade)
        session.commit()
        logger.info(
            "Paper trade created: %s %s @ ₹%.2f (Stop: ₹%.2f, Target: ₹%.2f, Tier: %s, Score: %s)",
            symbol, direction, entry_price, stop_loss, target_price, alert_tier, confluence_score,
        )
        return trade
    except Exception as exc:
        session.rollback()
        logger.error("Error creating paper trade entry for %s: %s", symbol, exc, exc_info=True)
        return None


def monitor_open_paper_trades(
    session: Session,
    quotes_by_symbol: dict[str, dict[str, Any]],
    notifiers: list[BaseNotifier],
    config: dict[str, Any],
    session_date: Optional[date] = None,
) -> list[PaperTrade]:
    """
    Check all open paper trades against current market quotes.
    Updates MFE/MAE, executes stop/target/time exits, and fires milestone alerts.
    """
    pt_cfg = config.get("paper_trading", {})
    if not pt_cfg.get("enabled", True):
        return []

    cur_date = session_date or date.today()
    max_hold_days = int(pt_cfg.get("max_hold_days", 10))
    milestones = pt_cfg.get("milestones", [2.5, -2.5, 5.0, -5.0])
    send_milestones = pt_cfg.get("send_milestone_alerts", True)
    send_exits = pt_cfg.get("send_exit_alerts", True)

    open_trades = session.query(PaperTrade).filter_by(status="open").all()
    if not open_trades:
        return []

    updated_trades: list[PaperTrade] = []
    for trade in open_trades:
        quote = quotes_by_symbol.get(trade.symbol)
        if not quote:
            continue
        try:
            current_price = float(quote.get("last_price", 0.0))
        except (ValueError, TypeError):
            continue

        if current_price <= 0.0:
            continue

        # Excursion tracking & P&L computation
        if trade.direction == "BUY":
            pnl_pct = ((current_price - trade.entry_price) / trade.entry_price) * 100.0
            trade.max_favorable_excursion = max(
                trade.max_favorable_excursion if trade.max_favorable_excursion is not None else current_price,
                current_price,
            )
            trade.max_adverse_excursion = min(
                trade.max_adverse_excursion if trade.max_adverse_excursion is not None else current_price,
                current_price,
            )
        else:  # SELL
            pnl_pct = ((trade.entry_price - current_price) / trade.entry_price) * 100.0
            trade.max_favorable_excursion = min(
                trade.max_favorable_excursion if trade.max_favorable_excursion is not None else current_price,
                current_price,
            )
            trade.max_adverse_excursion = max(
                trade.max_adverse_excursion if trade.max_adverse_excursion is not None else current_price,
                current_price,
            )

        # Check exit conditions
        exit_reason = None
        if trade.direction == "BUY":
            if current_price <= trade.stop_loss:
                exit_reason = "stop_hit"
            elif current_price >= trade.target_price:
                exit_reason = "target_hit"
        else:  # SELL
            if current_price >= trade.stop_loss:
                exit_reason = "stop_hit"
            elif current_price <= trade.target_price:
                exit_reason = "target_hit"

        days_held = (cur_date - trade.entry_date).days
        if not exit_reason and days_held >= max_hold_days:
            exit_reason = "time_expiry"

        if exit_reason:
            trade.exit_price = round(current_price, 2)
            trade.exit_date = cur_date
            trade.exit_datetime = datetime.now(timezone.utc)
            trade.exit_reason = exit_reason
            trade.status = "closed"
            if trade.direction == "BUY":
                trade.pnl_absolute = round(current_price - trade.entry_price, 2)
            else:
                trade.pnl_absolute = round(trade.entry_price - current_price, 2)
            trade.pnl_percent = round((trade.pnl_absolute / trade.entry_price) * 100.0, 2)
            if trade.risk_distance and trade.risk_distance > 0:
                trade.r_multiple = round(trade.pnl_absolute / trade.risk_distance, 2)
            trade.updated_at = datetime.now(timezone.utc)
            session.commit()
            logger.info(
                "Paper trade closed: %s %s (%s) @ ₹%.2f | P&L: %+.2f%% (%+.2fR)",
                trade.symbol, trade.direction, exit_reason, current_price,
                trade.pnl_percent, trade.r_multiple or 0.0,
            )
            if send_exits:
                _send_exit_notification(trade, notifiers)
        else:
            trade.updated_at = datetime.now(timezone.utc)
            session.commit()
            if send_milestones:
                _check_and_fire_milestones(session, trade, current_price, pnl_pct, milestones, notifiers)

        updated_trades.append(trade)

    return updated_trades


def _check_and_fire_milestones(
    session: Session,
    trade: PaperTrade,
    current_price: float,
    pnl_pct: float,
    milestones: list[float],
    notifiers: list[BaseNotifier],
) -> None:
    """Check milestone thresholds (±2.5%, ±5%) and fire notification once per milestone."""
    for m in milestones:
        is_hit = (m > 0 and pnl_pct >= m) or (m < 0 and pnl_pct <= m)
        if not is_hit:
            continue

        existing = session.query(PaperTradeMilestone).filter_by(
            trade_id=trade.id, milestone_pct=float(m)
        ).first()
        if existing:
            continue

        milestone_rec = PaperTradeMilestone(
            trade_id=trade.id,
            milestone_pct=float(m),
            hit_price=round(current_price, 2),
            hit_datetime=datetime.now(timezone.utc),
            notified=False,
        )
        session.add(milestone_rec)
        session.commit()

        emoji = "🟢" if m > 0 else "🔴"
        sign = "+" if m > 0 else ""
        tier_label = trade.alert_tier.replace("_", " ").title() if trade.alert_tier else "Alert"
        msg = (
            f"{emoji} <b>Paper Trade Milestone Hit: {sign}{m:.1f}%</b>\n"
            f"─────────────────────\n"
            f"📌 <b>{trade.symbol}</b> ({trade.direction}) · {tier_label}\n"
            f"💵 Entry: ₹{trade.entry_price:,.2f} → Current: ₹{current_price:,.2f}\n"
            f"📈 Current P&L: <b>{pnl_pct:+.2f}%</b>\n"
            f"🛑 Stop: ₹{trade.stop_loss:,.2f} | 🎯 Target: ₹{trade.target_price:,.2f}\n"
            f"📅 Entry Date: {trade.entry_date}\n"
            f"─────────────────────\n"
            f"<i>Paper trade validation tracker</i>"
        )
        sent = False
        for notifier in notifiers:
            try:
                if notifier.broadcast_message(msg):
                    sent = True
            except Exception as e:
                logger.warning("Failed to broadcast milestone alert: %s", e)

        if sent:
            milestone_rec.notified = True
            milestone_rec.notified_at = datetime.now(timezone.utc)
            session.commit()


def _send_exit_notification(trade: PaperTrade, notifiers: list[BaseNotifier]) -> None:
    """Send trade closure alert."""
    emoji = "🎯" if trade.exit_reason == "target_hit" else ("🛑" if trade.exit_reason == "stop_hit" else "⏳")
    reason_name = trade.exit_reason.replace("_", " ").title() if trade.exit_reason else "Closed"
    pnl_sign = "+" if (trade.pnl_percent or 0) > 0 else ""
    r_text = f" ({trade.r_multiple:+.1f}R)" if trade.r_multiple is not None else ""
    days_held = (trade.exit_date - trade.entry_date).days if trade.exit_date else 0
    msg = (
        f"{emoji} <b>Paper Trade Closed: {reason_name}</b>\n"
        f"─────────────────────\n"
        f"📌 <b>{trade.symbol}</b> ({trade.direction})\n"
        f"💵 Entry: ₹{trade.entry_price:,.2f} → Exit: ₹{trade.exit_price:,.2f}\n"
        f"📊 Result: <b>{pnl_sign}{trade.pnl_percent:.2f}%{r_text}</b> (₹{trade.pnl_absolute:+,.2f})\n"
        f"🛑 Stop: ₹{trade.stop_loss:,.2f} | 🎯 Target: ₹{trade.target_price:,.2f}\n"
        f"⏱️ Duration: {days_held} days\n"
        f"─────────────────────\n"
        f"<i>Paper trade validation tracker</i>"
    )
    for notifier in notifiers:
        try:
            notifier.broadcast_message(msg)
        except Exception as e:
            logger.warning("Failed to broadcast exit alert: %s", e)


def generate_weekly_report(
    session: Session,
    notifiers: list[BaseNotifier],
    config: dict[str, Any],
    week_start_date: Optional[date] = None,
    week_end_date: Optional[date] = None,
    send_notification: bool = True,
) -> dict[str, Any]:
    """
    Compile and broadcast weekly paper trade performance scorecard.
    Stores aggregated statistics in PaperTradeWeeklyReport.
    """
    today = date.today()
    if week_start_date is not None and week_end_date is not None:
        week_start, week_end = week_start_date, week_end_date
    else:
        # Default: target the trading week (Monday to Friday)
        weekday = today.weekday()  # Monday=0, Sunday=6
        if weekday == 5:  # Saturday
            week_end = today - timedelta(days=1)
            week_start = week_end - timedelta(days=4)
        elif weekday == 6:  # Sunday
            week_end = today - timedelta(days=2)
            week_start = week_end - timedelta(days=4)
        else:
            # Fallback for manual trigger mid-week: current week so far
            week_start = today - timedelta(days=weekday)
            week_end = week_start + timedelta(days=4)

    # Query trades relevant to this week
    trades_in_window = session.query(PaperTrade).filter(
        PaperTrade.entry_date <= week_end,
        or_(PaperTrade.exit_date.is_(None), PaperTrade.exit_date >= week_start),
    ).all()

    closed_this_week = [
        t for t in trades_in_window
        if t.status == "closed" and t.exit_date and week_start <= t.exit_date <= week_end
    ]
    still_open = [t for t in trades_in_window if t.status == "open"]
    new_entries = [t for t in trades_in_window if week_start <= t.entry_date <= week_end]

    winners = [t for t in closed_this_week if (t.pnl_percent or 0.0) > 0.0]
    losers = [t for t in closed_this_week if (t.pnl_percent or 0.0) <= 0.0]

    win_rate = (len(winners) / len(closed_this_week) * 100.0) if closed_this_week else 0.0
    avg_pnl = (sum(t.pnl_percent for t in closed_this_week) / len(closed_this_week)) if closed_this_week else 0.0
    avg_r = (sum(t.r_multiple or 0.0 for t in closed_this_week) / len(closed_this_week)) if closed_this_week else 0.0

    best_trade = max(closed_this_week, key=lambda t: t.pnl_percent or -999.0) if closed_this_week else None
    worst_trade = min(closed_this_week, key=lambda t: t.pnl_percent or 999.0) if closed_this_week else None

    # Tier breakdown
    radar_closed = [t for t in closed_this_week if t.alert_tier == "intraday_radar"]
    radar_win_rate = (sum(1 for t in radar_closed if (t.pnl_percent or 0) > 0) / len(radar_closed) * 100.0) if radar_closed else None

    confirmed_closed = [t for t in closed_this_week if t.alert_tier in {"confirmed_daily", "power_hour_confirmed"}]
    confirmed_win_rate = (sum(1 for t in confirmed_closed if (t.pnl_percent or 0) > 0) / len(confirmed_closed) * 100.0) if confirmed_closed else None

    # Score breakdown
    score_2_closed = [t for t in closed_this_week if t.confluence_score == 2]
    score_2_win_rate = (sum(1 for t in score_2_closed if (t.pnl_percent or 0) > 0) / len(score_2_closed) * 100.0) if score_2_closed else None

    score_3_closed = [t for t in closed_this_week if t.confluence_score == 3]
    score_3_win_rate = (sum(1 for t in score_3_closed if (t.pnl_percent or 0) > 0) / len(score_3_closed) * 100.0) if score_3_closed else None

    report_data = {
        "week": f"{week_start.isoformat()} to {week_end.isoformat()}",
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "total_active_window_trades": len(trades_in_window),
        "new_entries": len(new_entries),
        "closed_trades": len(closed_this_week),
        "open_trades": len(still_open),
        "winners": len(winners),
        "losers": len(losers),
        "win_rate_pct": round(win_rate, 1) if closed_this_week else None,
        "avg_pnl_pct": round(avg_pnl, 2) if closed_this_week else None,
        "avg_r_multiple": round(avg_r, 2) if closed_this_week else None,
        "best_trade": {
            "symbol": best_trade.symbol,
            "pnl_pct": best_trade.pnl_percent,
            "r_multiple": best_trade.r_multiple,
        } if best_trade else None,
        "worst_trade": {
            "symbol": worst_trade.symbol,
            "pnl_pct": worst_trade.pnl_percent,
            "r_multiple": worst_trade.r_multiple,
        } if worst_trade else None,
        "tier_stats": {
            "radar_trades": len(radar_closed),
            "radar_win_rate": round(radar_win_rate, 1) if radar_win_rate is not None else None,
            "confirmed_trades": len(confirmed_closed),
            "confirmed_win_rate": round(confirmed_win_rate, 1) if confirmed_win_rate is not None else None,
        },
        "score_stats": {
            "score_2_trades": len(score_2_closed),
            "score_2_win_rate": round(score_2_win_rate, 1) if score_2_win_rate is not None else None,
            "score_3_trades": len(score_3_closed),
            "score_3_win_rate": round(score_3_win_rate, 1) if score_3_win_rate is not None else None,
        },
    }

    # Format Telegram broadcast message
    lines = [
        "📊 <b>Weekly Paper Trading Report</b>",
        "─────────────────────",
        f"📅 <b>Week:</b> {week_start} to {week_end}",
        "",
        "📈 <b>Summary:</b>",
        f"• New Entries: <b>{len(new_entries)}</b> | Closed: <b>{len(closed_this_week)}</b> | Open: <b>{len(still_open)}</b>",
    ]
    if closed_this_week:
        lines.extend([
            f"• Winners: <b>{len(winners)}</b> | Losers: <b>{len(losers)}</b>",
            f"• Win Rate: <b>{win_rate:.1f}%</b>",
            f"• Avg P&L: <b>{avg_pnl:+.2f}%</b> (Avg R: <b>{avg_r:+.2f}R</b>)",
        ])
        if best_trade:
            lines.append(f"🏆 Best: <b>{best_trade.symbol}</b> ({best_trade.pnl_percent:+.2f}%, {best_trade.r_multiple or 0:+.1f}R)")
        if worst_trade:
            lines.append(f"💀 Worst: <b>{worst_trade.symbol}</b> ({worst_trade.pnl_percent:+.2f}%, {worst_trade.r_multiple or 0:+.1f}R)")
        lines.append("")
        lines.append("🎯 <b>By Alert Tier:</b>")
        if radar_closed:
            lines.append(f"• Radar: {len(radar_closed)} trades ({radar_win_rate:.1f}% win rate)")
        if confirmed_closed:
            lines.append(f"• Confirmed Entry: {len(confirmed_closed)} trades ({confirmed_win_rate:.1f}% win rate)")
        lines.append("")
        lines.append("🔢 <b>By Confluence Score:</b>")
        if score_2_closed:
            lines.append(f"• Score 2: {len(score_2_closed)} trades ({score_2_win_rate:.1f}% win rate)")
        if score_3_closed:
            lines.append(f"• Score 3: {len(score_3_closed)} trades ({score_3_win_rate:.1f}% win rate)")
    else:
        lines.append("<i>No trades closed during this week.</i>")

    if still_open:
        lines.append("")
        lines.append(f"📌 <b>Open Positions ({len(still_open)}):</b>")
        for ot in still_open[:10]:
            lines.append(f"• {ot.symbol} ({ot.direction}) @ ₹{ot.entry_price:,.2f} [Stop: ₹{ot.stop_loss:,.2f}]")
        if len(still_open) > 10:
            lines.append(f"<i>... and {len(still_open) - 10} more</i>")

    lines.append("─────────────────────")
    lines.append("<i>Paper trading validation scorecard</i>")
    report_text = "\n".join(lines)

    if send_notification:
        for notifier in notifiers:
            try:
                notifier.broadcast_message(report_text)
            except Exception as e:
                logger.warning("Failed to broadcast weekly report: %s", e)

    # Persist or update record in DB
    existing_rep = session.query(PaperTradeWeeklyReport).filter_by(week_start=week_start).first()
    if existing_rep is None:
        rep_record = PaperTradeWeeklyReport(
            week_start=week_start,
            week_end=week_end,
            total_trades=len(trades_in_window),
            open_trades=len(still_open),
            closed_trades=len(closed_this_week),
            winners=len(winners),
            losers=len(losers),
            win_rate_pct=round(win_rate, 1) if closed_this_week else None,
            avg_pnl_pct=round(avg_pnl, 2) if closed_this_week else None,
            avg_r_multiple=round(avg_r, 2) if closed_this_week else None,
            best_trade_symbol=best_trade.symbol if best_trade else None,
            best_trade_pnl_pct=best_trade.pnl_percent if best_trade else None,
            worst_trade_symbol=worst_trade.symbol if worst_trade else None,
            worst_trade_pnl_pct=worst_trade.pnl_percent if worst_trade else None,
            radar_trades=len(radar_closed),
            radar_win_rate=round(radar_win_rate, 1) if radar_win_rate is not None else None,
            confirmed_trades=len(confirmed_closed),
            confirmed_win_rate=round(confirmed_win_rate, 1) if confirmed_win_rate is not None else None,
            score_2_trades=len(score_2_closed),
            score_2_win_rate=round(score_2_win_rate, 1) if score_2_win_rate is not None else None,
            score_3_trades=len(score_3_closed),
            score_3_win_rate=round(score_3_win_rate, 1) if score_3_win_rate is not None else None,
            report_json=json.dumps(report_data),
            generated_at=datetime.now(timezone.utc),
        )
        session.add(rep_record)
    else:
        existing_rep.total_trades = len(trades_in_window)
        existing_rep.open_trades = len(still_open)
        existing_rep.closed_trades = len(closed_this_week)
        existing_rep.winners = len(winners)
        existing_rep.losers = len(losers)
        existing_rep.win_rate_pct = round(win_rate, 1) if closed_this_week else None
        existing_rep.avg_pnl_pct = round(avg_pnl, 2) if closed_this_week else None
        existing_rep.avg_r_multiple = round(avg_r, 2) if closed_this_week else None
        existing_rep.best_trade_symbol = best_trade.symbol if best_trade else None
        existing_rep.best_trade_pnl_pct = best_trade.pnl_percent if best_trade else None
        existing_rep.worst_trade_symbol = worst_trade.symbol if worst_trade else None
        existing_rep.worst_trade_pnl_pct = worst_trade.pnl_percent if worst_trade else None
        existing_rep.radar_trades = len(radar_closed)
        existing_rep.radar_win_rate = round(radar_win_rate, 1) if radar_win_rate is not None else None
        existing_rep.confirmed_trades = len(confirmed_closed)
        existing_rep.confirmed_win_rate = round(confirmed_win_rate, 1) if confirmed_win_rate is not None else None
        existing_rep.score_2_trades = len(score_2_closed)
        existing_rep.score_2_win_rate = round(score_2_win_rate, 1) if score_2_win_rate is not None else None
        existing_rep.score_3_trades = len(score_3_closed)
        existing_rep.score_3_win_rate = round(score_3_win_rate, 1) if score_3_win_rate is not None else None
        existing_rep.report_json = json.dumps(report_data)
        existing_rep.generated_at = datetime.now(timezone.utc)

    session.commit()
    report_data["formatted_message"] = report_text
    return report_data


if __name__ == "__main__":
    import argparse
    from src.config_loader import get_config
    from src.notifiers import get_notifiers

    parser = argparse.ArgumentParser(description="Paper trading management CLI")
    parser.add_argument("--weekly-report", action="store_true", help="Generate and broadcast weekly report")
    parser.add_argument("--status", action="store_true", help="Show current open paper trades")
    args = parser.parse_args()

    cfg = get_config()
    session = get_session()
    notifiers = get_notifiers(cfg)

    try:
        if args.weekly_report:
            report = generate_weekly_report(session, notifiers, cfg, send_notification=True)
            print("Weekly Report:")
            print(report.get("formatted_message"))
        elif args.status:
            trades = session.query(PaperTrade).filter_by(status="open").all()
            print(f"Open Paper Trades ({len(trades)}):")
            for t in trades:
                print(f"  {t.symbol} {t.direction} @ ₹{t.entry_price:.2f} (Entered: {t.entry_date}, Stop: ₹{t.stop_loss:.2f}, Target: ₹{t.target_price:.2f})")
        else:
            parser.print_help()
    finally:
        session.close()
