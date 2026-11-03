"""
Tests for confluence scoring and Screener fundamentals parsing.
"""
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import pytest

from src.fetcher.fundamentals import _parse_indian_number, classify_cap
from src.notifiers.base import AlertPayload
from src.notifiers.telegram import TelegramNotifier
from src.scanner import Scanner
from src.db import Stock


def test_parse_indian_number():
    assert _parse_indian_number("17,71,283") == 1771283.0
    assert _parse_indian_number("₹17,71,283Cr.") == 1771283.0
    assert _parse_indian_number("₹ 3,450.50 Cr") == 3450.5
    assert _parse_indian_number("15.2%") == 15.2
    assert _parse_indian_number("1234.56") == 1234.56
    assert _parse_indian_number("") is None
    assert _parse_indian_number("-") is None
    assert _parse_indian_number(None) is None


def test_classify_cap():
    assert classify_cap(25000) == "large"
    assert classify_cap(10000) == "mid"
    assert classify_cap(1500) == "small"
    assert classify_cap(200) == "micro"
    assert classify_cap(None) == "unknown"


def test_telegram_message_formatting_confluence():
    cfg = {
        "notifications": {
            "telegram": {
                "bot_token": "dummy_token",
                "chat_id": "dummy_chat_id",
            }
        }
    }
    notifier = TelegramNotifier(cfg)
    payload = AlertPayload(
        symbol="RELIANCE",
        company_name="Reliance Industries Ltd",
        signal_type="confluence_buy",
        direction="BUY",
        price=1308.0,
        indicator_value=3.0,
        indicator_detail={},
        confluence_score=3,
        total_signals=5,
        checks=[
            {
                "name": "RSI (14)",
                "signal_type": "rsi",
                "met": True,
                "direction": "BUY",
                "value_text": "RSI 28.4",
                "description": "Oversold (≤ 30)",
            },
            {
                "name": "MACD (12,26,9)",
                "signal_type": "macd",
                "met": True,
                "direction": "BUY",
                "value_text": "MACD +1.20",
                "description": "Bullish Crossover",
            },
            {
                "name": "EMA 9×21",
                "signal_type": "ema_9x21",
                "met": True,
                "direction": "BUY",
                "value_text": "₹1300 vs ₹1295",
                "description": "Buy Cross",
            },
            {
                "name": "EMA 21×50",
                "signal_type": "ema_21x50",
                "met": False,
                "direction": None,
                "value_text": "₹1295 (21<50)",
                "description": "No Cross",
            },
            {
                "name": "Volume Breakout",
                "signal_type": "volume_breakout",
                "met": False,
                "direction": None,
                "value_text": "0.9× Avg Vol",
                "description": "Normal Volume",
            },
        ],
        mcap_cr=1771283.0,
        cap_category="large",
        sector="Energy",
        pe_ratio=21.6,
        pb_ratio=1.87,
        roe=8.9,
    )
    msg = notifier._format_message(payload)
    assert "RELIANCE" in msg
    assert "BUY Signal (3/5 Confluence)" in msg
    assert "✅ <b>RSI (14)</b>: Oversold (≤ 30) (RSI 28.4)" in msg
    assert "✅ <b>MACD (12,26,9)</b>: Bullish Crossover" in msg
    assert "❌ EMA 21×50: No Cross" in msg
    assert "Market Cap: <b>₹17.7L Cr</b> (Large Cap)" in msg
    assert "Energy" in msg
