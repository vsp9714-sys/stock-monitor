"""
Telegram notifier — sends alerts with chart images via Telegram Bot API.

Features:
- Rich formatted message (HTML parse mode)
- Chart image attachment via sendPhoto
- Configurable chat_id (personal / group / channel)
- Retry on transient errors

Setup:
1. Create a bot via @BotFather on Telegram
2. Get the bot token
3. Start a chat with the bot (or add to a group/channel)
4. Get your chat_id via: https://api.telegram.org/bot<TOKEN>/getUpdates
5. Add to config/secrets.yaml
"""
import html
import logging
import time
from pathlib import Path
from typing import Optional

import requests

from src.notifiers.base import AlertPayload, BaseNotifier

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}"


class TelegramNotifier(BaseNotifier):
    """Sends alerts to a Telegram chat."""

    def __init__(self, cfg: dict):
        tg = cfg.get("notifications", {}).get("telegram", {})
        self.bot_token = tg.get("bot_token", "")
        self.chat_id = tg.get("chat_id", "")
        self.group_chat_id = tg.get("group_chat_id", "")
        self.destinations: dict[str, str] = {}
        self.send_chart_cfg = tg.get("send_chart", True)

        if not self.bot_token:
            raise ValueError("Telegram bot_token must be configured in secrets.yaml")

        if _config_switch(tg.get("individual_alert", "Y")):
            if not self.chat_id:
                raise ValueError("Telegram individual_alert is enabled but chat_id is empty")
            self.destinations["telegram:individual"] = str(self.chat_id)
        if _config_switch(tg.get("group_alert", "N")):
            if not self.group_chat_id:
                raise ValueError("Telegram group_alert is enabled but group_chat_id is empty")
            self.destinations["telegram:group"] = str(self.group_chat_id)
        if not self.destinations:
            raise ValueError("Enable individual_alert or group_alert for Telegram")

        self._base_url = TELEGRAM_API.format(token=self.bot_token)

    def delivery_targets(self) -> list[str]:
        return list(self.destinations)

    # ─── Message Formatting ───────────────────────────────────────────────

    def _format_message(self, payload: AlertPayload) -> str:
        """Format a rich HTML message for Telegram."""
        direction_emoji = "🟢" if payload.direction == "BUY" else ("🔴" if payload.direction == "SELL" else "🟡")
        cap_emoji = {"large": "🏔️", "mid": "🏞️", "small": "🌲", "micro": "🌱"}.get(
            payload.cap_category or "", "📊"
        )

        confluence_tag = f" ({payload.confluence_score}/{payload.total_signals} groups)" if payload.total_signals > 1 else ""

        tier = payload.alert_tier or (
            "power_hour_confirmed" if "power_hour" in payload.signal_type
            else ("intraday_radar" if payload.provisional else "confirmed_daily")
        )

        if tier == "power_hour_confirmed":
            header_line = f"🟢 <b>{payload.symbol}</b> — {payload.direction} Signal{confluence_tag} [3:15 PM CONFIRMED ENTRY]"
            tier_badge = "🎯 <b>3:15 PM Confirmation Window</b> — High-conviction close (CLV verified); review and enter before 15:30 IST"
        elif tier == "intraday_radar":
            header_line = f"🟡 <b>{payload.symbol}</b> — {payload.direction} Signal{confluence_tag} [INTRADAY RADAR]"
            tier_badge = "⚠️ <b>Watchlist Setup (Do Not Chase)</b> — Intraday move in progress; watch for 3:15 PM confirmation"
        else:
            header_line = f"{direction_emoji} <b>{payload.symbol}</b> — {payload.direction} Signal{confluence_tag}"
            tier_badge = f"📌 <b>{payload.format_signal_label()}</b>"

        lines = [
            header_line,
            tier_badge,
            "",
            f"{'⚠️' if payload.provisional else 'ℹ️'} <i>{html.escape(payload.format_signal_provenance())}</i>",
            f"🏢 <b>{payload.company_name}</b>",
            f"💰 Price: <b>₹{payload.price:,.2f}</b> ({html.escape(payload.price_source)})",
            f"🕒 Quote time: {html.escape(str(payload.quote_timestamp) if payload.quote_timestamp else 'unknown')}",
        ]

        # Candle strength / CLV if available
        clv_val = payload.clv
        if clv_val is None and isinstance(payload.indicator_detail, dict):
            clv_val = payload.indicator_detail.get("clv")
        if clv_val is not None:
            clv_pct = clv_val * 100
            strength_desc = (
                "Top of day range" if clv_val >= 0.70
                else ("Bottom of day range" if clv_val <= 0.30 else "Mid range")
            )
            lines.append(f"🕯️ Candle Bar Strength: <b>{clv_pct:.0f}%</b> ({strength_desc}; CLV {clv_val:.2f})")

        # Multi-signal check list
        if payload.checks:
            lines.append("")
            lines.append(
                f"🎯 <b>Independent Groups ({payload.confluence_score}/{payload.total_signals}; "
                f"{html.escape(payload.confluence_mode)} mode)</b>"
            )
            for chk in payload.checks:
                name_esc = html.escape(str(chk.get('name', '')))
                desc_esc = html.escape(str(chk.get('description', '')))
                val_esc = html.escape(str(chk.get('value_text', '')))
                if chk.get("availability") == "unavailable":
                    icon = payload.check_status_icon(chk)
                    lines.append(f"{icon} <b>{name_esc}</b>: {desc_esc} ({val_esc})")
                elif chk.get("met"):
                    icon = payload.check_status_icon(chk)
                    lines.append(f"{icon} <b>{name_esc}</b>: {desc_esc} ({val_esc})")
                else:
                    icon = payload.check_status_icon(chk)
                    lines.append(f"{icon} {name_esc}: {desc_esc} ({val_esc})")
        elif payload.indicator_value is not None:
            ind_label = self._indicator_label(payload.signal_type)
            lines.append(f"📈 {ind_label}: <b>{payload.indicator_value:.2f}</b>")

        lines.append("")
        lines.append("─────────────────────")
        lines.append("📊 <b>Fundamentals</b>")

        if payload.mcap_cr is not None:
            lines.append(f"{cap_emoji} Market Cap: <b>{payload.format_mcap()}</b> ({(payload.cap_category or 'N/A').title()} Cap)")
        if payload.sector:
            sec_text = payload.sector
            if payload.industry and payload.industry != payload.sector:
                sec_text += f" ({payload.industry})"
            lines.append(f"🏭 Sector: {sec_text}")
        if payload.fundamentals_fetched_at or payload.fundamentals_freshness != "unavailable":
            age = f"{payload.fundamentals_cache_age_hours:.1f}h" if payload.fundamentals_cache_age_hours is not None else "unknown age"
            lines.append(
                f"🧾 Fundamentals: {html.escape(payload.fundamentals_freshness)} · "
                f"{html.escape(payload.fundamentals_data_basis)} · {age} · "
                f"fetched {html.escape(payload.fundamentals_fetched_at or 'unknown')}"
            )
        if payload.market_regime or payload.market_relative_strength_pct is not None:
            strength = (
                f" · relative strength {payload.market_relative_strength_pct:+.2f}% vs Nifty 50"
                if payload.market_relative_strength_pct is not None else ""
            )
            lines.append(f"🌐 Market regime: {html.escape(payload.market_regime or 'unavailable')}{strength}")
        if payload.sector_regime or payload.sector_relative_strength_pct is not None:
            strength = (
                f" · relative strength {payload.sector_relative_strength_pct:+.2f}% vs sector index"
                if payload.sector_relative_strength_pct is not None else ""
            )
            lines.append(f"🏭 Sector regime: {html.escape(payload.sector_regime or 'unavailable')}{strength}")
        if payload.derivatives_context:
            oi = payload.derivatives_context
            if oi.get("status") == "available":
                lines.append(
                    f"📊 Options OI ({html.escape(str(oi.get('expiry')))}): "
                    f"PCR {float(oi['put_call_oi_ratio']):.2f} · "
                    f"total OI change {float(oi['total_oi_change_pct']):+.1f}% · "
                    f"retrieved {html.escape(str(oi.get('retrieved_at', 'unknown')))}"
                    if oi.get("total_oi_change_pct") is not None else
                    f"📊 Options OI ({html.escape(str(oi.get('expiry')))}): PCR {float(oi['put_call_oi_ratio']):.2f} · "
                    f"retrieved {html.escape(str(oi.get('retrieved_at', 'unknown')))}"
                )
            elif oi.get("status") == "unavailable":
                lines.append(f"📊 Options OI: unavailable ({html.escape(str(oi.get('reason', 'unknown')))}).")
        risk = payload.risk_context or {}
        if risk.get("status") == "available":
            size = (
                f" · quantity {risk['quantity']} (risk budget ₹{risk.get('risk_budget_inr', 0):,.0f})"
                if risk.get("position_size_status") == "available" else
                " · quantity not configured" if risk.get("position_size_status") == "unconfigured" else ""
            )
            lines.append(
                f"⚠️ Model risk references only (not an order): stop {risk['stop_atr_multiple']:.2f}×ATR "
                f"(₹{risk['stop_reference']:.2f}) · "
                f"target ₹{risk['target_reference']:.2f} · {risk['reward_risk_ratio']:.1f}R · "
                f"ATR({risk['atr_period']}) ₹{risk['atr']:.2f}{size}"
            )
        elif risk.get("status") == "unavailable":
            lines.append(f"⚠️ Model risk reference unavailable ({html.escape(str(risk.get('reason', 'unknown')))}).")
        if payload.pe_ratio is not None:
            lines.append(f"📐 P/E Ratio: <b>{payload.pe_ratio:.1f}x</b>")
        if payload.pb_ratio is not None:
            lines.append(f"📐 P/B Ratio: <b>{payload.pb_ratio:.1f}x</b>")
        if payload.roe is not None:
            lines.append(f"🎯 ROE: <b>{payload.roe:.1f}%</b>")
        if payload.promoter_holding_pct is not None:
            lines.append(f"👤 Promoter Holding: <b>{payload.promoter_holding_pct:.1f}%</b>")

        profit_trend = payload.format_profit_trend()
        if profit_trend != "N/A":
            lines.append("")
            lines.append("📉 <b>Quarterly Net Profit (Cr)</b>")
            lines.append(f"<code>{profit_trend}</code>")

        lines.append("─────────────────────")

        if payload.screener_url:
            lines.append(f"🔗 <a href=\"{payload.screener_url}\">View on Screener.in</a>")

        lines.append(f"\n⏰ <i>Alert generated at market scan</i>")

        return "\n".join(lines)

    @staticmethod
    def _indicator_label(signal_type: str) -> str:
        labels = {
            "rsi_oversold": "RSI",
            "rsi_overbought": "RSI",
            "macd_bullish_cross": "MACD",
            "macd_bearish_cross": "MACD",
            "volume_breakout_bullish": "Vol Ratio",
            "volume_breakout_bearish": "Vol Ratio",
            "volume_spike": "Vol Ratio",
        }
        return labels.get(signal_type, "Value")

    # ─── API Calls ────────────────────────────────────────────────────────

    def _send_message(self, text: str, chat_id: Optional[str] = None, retries: int = 3) -> bool:
        """Send a text message via Telegram."""
        if chat_id is None:
            if len(self.destinations) != 1:
                raise ValueError("chat_id is required when Telegram has multiple destinations")
            chat_id = next(iter(self.destinations.values()))
        url = f"{self._base_url}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }
        for attempt in range(retries):
            try:
                resp = requests.post(url, json=payload, timeout=30)
                if resp.ok:
                    return True
                logger.warning(f"Telegram sendMessage failed: {resp.status_code} {resp.text}")
            except requests.RequestException as e:
                logger.warning(f"Telegram request error (attempt {attempt+1}): {e}")
            time.sleep(2 ** attempt)
        return False

    def _send_photo(self, caption: str, photo_path: str, chat_id: Optional[str] = None, retries: int = 3) -> bool:
        """Send a photo with caption via Telegram."""
        if chat_id is None:
            if len(self.destinations) != 1:
                raise ValueError("chat_id is required when Telegram has multiple destinations")
            chat_id = next(iter(self.destinations.values()))
        url = f"{self._base_url}/sendPhoto"
        for attempt in range(retries):
            try:
                with open(photo_path, "rb") as photo:
                    resp = requests.post(
                        url,
                        data={
                            "chat_id": chat_id,
                            "caption": caption[:1024],  # Telegram caption limit
                            "parse_mode": "HTML",
                        },
                        files={"photo": photo},
                        timeout=60,
                    )
                if resp.ok:
                    return True
                logger.warning(f"Telegram sendPhoto failed: {resp.status_code} {resp.text}")
            except requests.RequestException as e:
                logger.warning(f"Telegram photo request error (attempt {attempt+1}): {e}")
            time.sleep(2 ** attempt)
        return False

    # ─── BaseNotifier interface ───────────────────────────────────────────

    def send_alert(self, payload: AlertPayload) -> bool:
        msg = self._format_message(payload)
        results = [self._send_message(msg, chat_id) for chat_id in self.destinations.values()]
        success = all(results)
        if success:
            logger.info(f"Telegram alert sent for {payload.symbol} [{payload.signal_type}]")
        return success

    def send_alert_with_chart(self, payload: AlertPayload) -> bool:
        results = [
            self.notify_target(payload, target, send_chart=True)
            for target in self.delivery_targets()
        ]
        return all(results)

    def notify_target(self, payload: AlertPayload, target: str, send_chart: bool = True) -> bool:
        """Deliver to an enabled individual or group chat independently."""
        chat_id = self.destinations.get(target)
        if not chat_id:
            raise ValueError(f"Unknown or disabled Telegram target: {target}")

        detail_msg = self._format_message(payload)
        text_sent = self._send_message(detail_msg, chat_id)
        photo_sent = False
        if send_chart and self.send_chart_cfg and payload.chart_path and Path(payload.chart_path).exists():
            confluence_tag = f" ({payload.confluence_score}/{payload.total_signals} groups)" if payload.total_signals > 1 else ""
            caption = (
                f"{('🟢' if payload.direction == 'BUY' else '🔴')} "
                f"<b>{payload.symbol}</b> — {payload.direction} Signal{confluence_tag}\n"
                f"Price: ₹{payload.price:,.2f} | "
                f"MCap: {payload.format_mcap()} ({(payload.cap_category or 'N/A').title()} Cap)"
            )
            photo_sent = self._send_photo(caption, payload.chart_path, chat_id)
        elif send_chart and self.send_chart_cfg and payload.chart_path:
            logger.warning(f"Chart not found for {payload.symbol}; text alert sent to {target} only.")

        success = text_sent or photo_sent
        if success:
            logger.info(f"Telegram alert sent for {payload.symbol} to {target}")
        return success


def _config_switch(value) -> bool:
    """Accept documented Y/N values and common YAML boolean equivalents."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"y", "yes", "true", "1", "on"}
