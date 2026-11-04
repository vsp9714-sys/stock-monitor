"""
Tests for confluence scoring and Screener fundamentals parsing.
"""
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
import pytest

from src.fetcher.fundamentals import _parse_indian_number, classify_cap
from src.fetcher.upstox import UpstoxFetcher
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


def test_nse_equity_master_parser_excludes_non_share_series():
    csv_text = (
        "SYMBOL,NAME OF COMPANY, SERIES,ISIN NUMBER\n"
        "ABC,ABC Limited,EQ,INE000A01000\n"
        "XYZ,XYZ Limited,BE,INE000B01000\n"
        "DEBT,Issuer 8.05% 2035,DEBT,INE000C01000\n"
    )
    parsed = UpstoxFetcher.parse_nse_equity_master(csv_text)
    assert set(parsed) == {"INE000A01000", "INE000B01000"}
    assert parsed["INE000A01000"]["symbol"] == "ABC"
    assert parsed["INE000B01000"]["series"] == "BE"


def test_nse_sme_equity_master_parser_normalizes_underscore_headers():
    csv_text = (
        "SYMBOL,NAME_OF_COMPANY,SERIES,ISIN_NUMBER\n"
        "SMALLCO,Small Company Limited,SM,INE000D01000\n"
        "SMALLRE,Small Company RE,ST,INE000E01000\n"
    )
    parsed = UpstoxFetcher.parse_nse_equity_master(csv_text)
    assert parsed["INE000D01000"]["series"] == "SM"
    assert parsed["INE000E01000"]["series"] == "ST"


def test_upstox_universe_matches_nse_equity_master_by_isin(monkeypatch):
    monkeypatch.setattr(UpstoxFetcher, "MIN_MATCHED_NSE_EQUITIES", 1)
    fetcher = object.__new__(UpstoxFetcher)
    fetcher.download_instruments = lambda exchange: pd.DataFrame([
        {"instrument_type": "EQUITY", "instrument_key": "NSE_EQ|INE000A01000",
         "tradingsymbol": "ABC", "name": "ABC Limited", "isin": "INE000A01000"},
        {"instrument_type": "EQUITY", "instrument_key": "NSE_EQ|INE000C01000",
         "tradingsymbol": "DEBT", "name": "Issuer 8.05% 2035", "isin": "INE000C01000"},
    ])
    fetcher.download_nse_equity_master = lambda: {
        "INE000A01000": {"symbol": "ABC", "company_name": "ABC Limited", "series": "EQ"},
    }
    rows = fetcher.get_all_eq_symbols("NSE")
    assert [row["symbol"] for row in rows] == ["ABC"]


def test_telegram_individual_and_group_routes_are_configurable(monkeypatch):
    cfg = {"notifications": {"telegram": {
        "bot_token": "dummy_token",
        "individual_alert": "N",
        "group_alert": "Y",
        "group_chat_id": "-1001234567890",
    }}}
    notifier = TelegramNotifier(cfg)
    assert notifier.delivery_targets() == ["telegram:group"]

    observed = {}

    class Response:
        ok = True

    def fake_post(url, json=None, data=None, files=None, timeout=None):
        observed.update(json or data or {})
        return Response()

    monkeypatch.setattr("src.notifiers.telegram.requests.post", fake_post)
    assert notifier._send_message("test", notifier.group_chat_id)
    assert observed["chat_id"] == "-1001234567890"
