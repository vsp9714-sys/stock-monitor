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
        self.send_chart_cfg = tg.get("send_chart", True)

        if not self.bot_token or not self.chat_id:
            raise ValueError(
                "Telegram bot_token and chat_id must be configured in secrets.yaml"
            )

        self._base_url = TELEGRAM_API.format(token=self.bot_token)

    # ─── Message Formatting ───────────────────────────────────────────────

    def _format_message(self, payload: AlertPayload) -> str:
        """Format a rich HTML message for Telegram."""
        direction_emoji = "🟢" if payload.direction == "BUY" else ("🔴" if payload.direction == "SELL" else "🟡")
        cap_emoji = {"large": "🏔️", "mid": "🏞️", "small": "🌲", "micro": "🌱"}.get(
            payload.cap_category or "", "📊"
        )

        lines = [
            f"{direction_emoji} <b>{payload.symbol}</b> — {payload.direction} Signal",
            f"📌 <b>{payload.format_signal_label()}</b>",
            "",
            f"🏢 <b>{payload.company_name}</b>",
            f"💰 Price: <b>₹{payload.price:,.2f}</b>",
        ]

        if payload.indicator_value is not None:
            ind_label = self._indicator_label(payload.signal_type)
            lines.append(f"📈 {ind_label}: <b>{payload.indicator_value:.2f}</b>")

        lines.append("")
        lines.append("─────────────────────")
        lines.append("📊 <b>Fundamentals</b>")

        if payload.mcap_cr is not None:
            lines.append(f"{cap_emoji} Market Cap: <b>{payload.format_mcap()}</b> ({(payload.cap_category or 'N/A').title()} Cap)")
        if payload.sector:
            lines.append(f"🏭 Sector: {payload.sector}")
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

    def _send_message(self, text: str, retries: int = 3) -> bool:
        """Send a text message via Telegram."""
        url = f"{self._base_url}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
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

    def _send_photo(self, caption: str, photo_path: str, retries: int = 3) -> bool:
        """Send a photo with caption via Telegram."""
        url = f"{self._base_url}/sendPhoto"
        for attempt in range(retries):
            try:
                with open(photo_path, "rb") as photo:
                    resp = requests.post(
                        url,
                        data={
                            "chat_id": self.chat_id,
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
        success = self._send_message(msg)
        if success:
            logger.info(f"Telegram alert sent for {payload.symbol} [{payload.signal_type}]")
        return success

    def send_alert_with_chart(self, payload: AlertPayload) -> bool:
        if not payload.chart_path or not Path(payload.chart_path).exists():
            logger.warning(f"Chart not found for {payload.symbol}, sending text-only.")
            return self.send_alert(payload)

        caption = (
            f"{('🟢' if payload.direction == 'BUY' else '🔴')} "
            f"<b>{payload.symbol}</b> — {payload.format_signal_label()}\n"
            f"Price: ₹{payload.price:,.2f} | "
            f"MCap: {payload.format_mcap()} ({(payload.cap_category or 'N/A').title()} Cap)"
        )

        # Send chart as photo with brief caption
        success = self._send_photo(caption, payload.chart_path)
        if success:
            # Send full detail as follow-up text message
            detail_msg = self._format_message(payload)
            self._send_message(detail_msg)
            logger.info(f"Telegram chart alert sent for {payload.symbol}")
        return success
