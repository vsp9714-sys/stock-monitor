"""
Discord notifier — sends alerts via Discord webhooks.

Features:
- Rich embed messages (with color based on BUY/SELL)
- Chart image upload via file attachment
- No bot token needed — just a webhook URL

Setup:
1. Go to your Discord server → Channel Settings → Integrations → Webhooks
2. Create a new webhook and copy the URL
3. Add to config/secrets.yaml
"""
import logging
from pathlib import Path
from typing import Optional

import requests

from src.notifiers.base import AlertPayload, BaseNotifier

logger = logging.getLogger(__name__)


class DiscordNotifier(BaseNotifier):
    """Sends alerts to a Discord channel via webhook."""

    def __init__(self, cfg: dict):
        dc = cfg.get("notifications", {}).get("discord", {})
        self.webhook_url = dc.get("webhook_url", "")
        self.send_chart_cfg = dc.get("send_chart", True)

        if not self.webhook_url:
            raise ValueError("Discord webhook_url must be configured in secrets.yaml")

    def _build_embed(self, payload: AlertPayload) -> dict:
        color = 0x3FB950 if payload.direction == "BUY" else 0xF85149  # Green/Red
        direction_emoji = "🟢" if payload.direction == "BUY" else "🔴"

        fields = [
            {"name": "Signal", "value": payload.format_signal_label(), "inline": True},
            {"name": "Direction", "value": f"{direction_emoji} {payload.direction}", "inline": True},
            {"name": "Price", "value": f"₹{payload.price:,.2f}", "inline": True},
        ]

        if payload.indicator_value is not None:
            fields.append({"name": "Indicator Value", "value": f"{payload.indicator_value:.2f}", "inline": True})

        if payload.mcap_cr is not None:
            fields.append({
                "name": "Market Cap",
                "value": f"{payload.format_mcap()} ({(payload.cap_category or 'N/A').title()} Cap)",
                "inline": True
            })

        if payload.sector:
            fields.append({"name": "Sector", "value": payload.sector, "inline": True})

        if payload.pe_ratio is not None:
            fields.append({"name": "P/E", "value": f"{payload.pe_ratio:.1f}x", "inline": True})
        if payload.roe is not None:
            fields.append({"name": "ROE", "value": f"{payload.roe:.1f}%", "inline": True})
        if payload.promoter_holding_pct is not None:
            fields.append({"name": "Promoter Holding", "value": f"{payload.promoter_holding_pct:.1f}%", "inline": True})

        profit_trend = payload.format_profit_trend()
        if profit_trend != "N/A":
            fields.append({"name": "Quarterly Profit (Last 4Q)", "value": f"`{profit_trend}`", "inline": False})

        embed = {
            "title": f"{payload.symbol} — {payload.company_name}",
            "color": color,
            "fields": fields,
            "footer": {"text": "Stock Monitor | Indian Market Alert"},
        }
        if payload.screener_url:
            embed["url"] = payload.screener_url

        return embed

    def send_alert(self, payload: AlertPayload) -> bool:
        embed = self._build_embed(payload)
        try:
            resp = requests.post(
                self.webhook_url,
                json={"embeds": [embed]},
                timeout=30,
            )
            resp.raise_for_status()
            logger.info(f"Discord alert sent for {payload.symbol}")
            return True
        except requests.RequestException as e:
            logger.error(f"Discord alert failed for {payload.symbol}: {e}")
            return False

    def send_alert_with_chart(self, payload: AlertPayload) -> bool:
        if not payload.chart_path or not Path(payload.chart_path).exists():
            return self.send_alert(payload)

        embed = self._build_embed(payload)
        try:
            with open(payload.chart_path, "rb") as chart:
                resp = requests.post(
                    self.webhook_url,
                    data={"payload_json": __import__("json").dumps({"embeds": [embed]})},
                    files={"file": ("chart.png", chart, "image/png")},
                    timeout=60,
                )
            resp.raise_for_status()
            logger.info(f"Discord chart alert sent for {payload.symbol}")
            return True
        except requests.RequestException as e:
            logger.error(f"Discord chart alert failed for {payload.symbol}: {e}")
            return False
