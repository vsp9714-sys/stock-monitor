"""
Base notifier — abstract interface for all notification channels.
All notifiers implement send_alert() and send_alert_with_chart().
"""
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class AlertPayload:
    """Structured alert data passed to notifiers."""
    symbol: str
    company_name: str
    signal_type: str
    direction: str                      # BUY / SELL / NEUTRAL
    price: float
    indicator_value: Optional[float]
    indicator_detail: dict              # Full indicator snapshot
    price_source: str = "Upstox last_price"
    quote_timestamp: Optional[str] = None

    # Confluence metadata
    confluence_score: int = 1
    total_signals: int = 1
    checks: list[dict] = field(default_factory=list)

    # Fundamental data (may be None if not available)
    mcap_cr: Optional[float] = None
    cap_category: Optional[str] = None
    sector: Optional[str] = None
    industry: Optional[str] = None
    pe_ratio: Optional[float] = None
    pb_ratio: Optional[float] = None
    roe: Optional[float] = None
    profit_q1_cr: Optional[float] = None
    profit_q2_cr: Optional[float] = None
    profit_q3_cr: Optional[float] = None
    profit_q4_cr: Optional[float] = None
    profit_ttm_cr: Optional[float] = None
    promoter_holding_pct: Optional[float] = None
    screener_url: Optional[str] = None

    # Chart path (optional)
    chart_path: Optional[str] = None

    def format_signal_label(self) -> str:
        if self.signal_type.startswith("confluence"):
            return f"Confluence {self.direction} ({self.confluence_score}/{self.total_signals} Signals Met)"
        labels = {
            "rsi_oversold": "RSI Oversold",
            "rsi_overbought": "RSI Overbought",
            "macd_bullish_cross": "MACD Bullish Crossover",
            "macd_bearish_cross": "MACD Bearish Crossover",
            "ema_9x21_bullish": "EMA 9×21 Bullish Cross",
            "ema_9x21_bearish": "EMA 9×21 Bearish Cross",
            "ema_21x50_bullish": "EMA 21×50 Bullish Cross",
            "ema_21x50_bearish": "EMA 21×50 Bearish Cross",
            "ema_50x200_bullish": "Golden Cross (EMA 50×200)",
            "ema_50x200_bearish": "Death Cross (EMA 50×200)",
            "volume_breakout_bullish": "Volume Breakout (Bullish)",
            "volume_breakout_bearish": "Volume Breakdown (Bearish)",
            "volume_spike": "Volume Spike",
        }
        return labels.get(self.signal_type, self.signal_type.replace("_", " ").title())

    def format_profit_trend(self) -> str:
        """Format last 4 quarters profit as trend string."""
        quarters = [self.profit_q4_cr, self.profit_q3_cr, self.profit_q2_cr, self.profit_q1_cr]
        labels = ["Q-3", "Q-2", "Q-1", "Q0"]
        parts = []
        for label, val in zip(labels, quarters):
            if val is not None:
                sign = "+" if val >= 0 else ""
                parts.append(f"{label}: {sign}{val:.0f}Cr")
        return " | ".join(parts) if parts else "N/A"

    def format_mcap(self) -> str:
        if self.mcap_cr is None:
            return "N/A"
        if self.mcap_cr >= 100_000:
            return f"₹{self.mcap_cr/100_000:.1f}L Cr"
        return f"₹{self.mcap_cr:,.0f} Cr"


class BaseNotifier(ABC):
    """Abstract base class for all notification channels."""

    @abstractmethod
    def send_alert(self, payload: AlertPayload) -> bool:
        """Send a text-only alert. Returns True on success."""
        pass

    @abstractmethod
    def send_alert_with_chart(self, payload: AlertPayload) -> bool:
        """Send an alert with chart image attached. Returns True on success."""
        pass

    def notify(self, payload: AlertPayload, send_chart: bool = True) -> bool:
        """High-level method: sends with chart if available, else text-only."""
        if send_chart and payload.chart_path:
            return self.send_alert_with_chart(payload)
        return self.send_alert(payload)

    def delivery_targets(self) -> list[str]:
        """Stable destination keys used for per-recipient escalation deduplication."""
        return [type(self).__name__.lower()]

    def notify_target(self, payload: AlertPayload, target: str, send_chart: bool = True) -> bool:
        """Deliver to one target; single-target notifiers use their normal path."""
        if target not in self.delivery_targets():
            raise ValueError(f"Unknown notification target: {target}")
        return self.notify(payload, send_chart=send_chart)
