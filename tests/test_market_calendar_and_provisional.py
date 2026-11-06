from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from src.market_calendar import CalendarUnavailable, is_market_open, is_trading_day
from src.notifiers.base import AlertPayload
from src.scanner import Scanner, _valid_ohlcv_frame, _validate_quote


def test_nse_calendar_uses_exchange_date_and_holiday_list():
    ist = ZoneInfo("Asia/Kolkata")
    assert is_trading_day(date(2026, 11, 6))
    assert not is_trading_day(date(2026, 11, 10))  # official NSE holiday
    assert not is_trading_day(date(2026, 11, 8))  # Muhurat hours have not been published
    assert not is_trading_day(date(2026, 11, 7))  # Saturday
    assert is_market_open(datetime(2026, 11, 6, 9, 16, tzinfo=ist))
    assert is_market_open(datetime(2026, 11, 6, 3, 46, tzinfo=timezone.utc))
    assert not is_market_open(datetime(2026, 11, 10, 10, 0, tzinfo=ist))


def test_unknown_calendar_year_fails_closed():
    try:
        is_trading_day(date(2027, 1, 4))
    except CalendarUnavailable:
        pass
    else:
        raise AssertionError("unknown NSE calendar year must fail closed")


def test_unavailable_check_is_not_rendered_as_a_failed_check():
    assert AlertPayload.check_status_icon({"met": False, "availability": "unavailable"}) == "➖"
    assert AlertPayload.check_status_icon({"met": False}) == "❌"
    assert AlertPayload.check_status_icon({"met": True}) == "✅"


def test_quote_and_cached_candle_sanity_reject_bad_values():
    now = datetime(2026, 11, 6, 4, 30, tzinfo=timezone.utc)
    epoch_ms = int(now.timestamp() * 1000)
    quote = {
        "open": 100, "high": 110, "low": 95, "close": 105, "volume": 1000,
        "last_price": 106, "prev_close_price": 98,
        "last_trade_time": epoch_ms, "ts": epoch_ms,
    }
    _, reason = _validate_quote(quote, now, 300)
    assert reason is None
    _, reason = _validate_quote({**quote, "low": 108}, now, 300)
    assert reason == "inconsistent_quote_ohlc"

    good = pd.DataFrame([{
        "ts": pd.Timestamp("2026-11-05"), "open": 100, "high": 110,
        "low": 95, "close": 105, "volume": 1000,
    }])
    assert _valid_ohlcv_frame(good)
    assert not _valid_ohlcv_frame(good.assign(volume=-1))
    assert not _valid_ohlcv_frame(good.assign(close=float("inf")))


def test_intraday_path_is_separate_and_provisional(monkeypatch):
    stock = type("StockRef", (), {
        "symbol": "AAA", "instrument_key": "NSE_EQ|AAA", "company_name": "AAA", "isin": "INE000A01000",
    })()
    now = datetime(2026, 11, 6, 4, 30, tzinfo=timezone.utc)  # 10:00 IST
    epoch_ms = int(now.timestamp() * 1000)

    class FakeQuery:
        def filter_by(self, **kwargs):
            return self

        def first(self):
            return None

    class FakeDB:
        def query(self, *args):
            return FakeQuery()

        def close(self):
            pass

    class Fetcher:
        def get_full_market_quotes(self, *args, **kwargs):
            return {"NSE_EQ|AAA": {
                "open": 100, "high": 110, "low": 95, "close": 105, "volume": 1000,
                "last_price": 106, "prev_close_price": 98,
                "last_trade_time": epoch_ms, "ts": epoch_ms,
            }}

    class Fundamentals:
        def get_fundamentals(self, *args):
            return {}

    scanner = object.__new__(Scanner)
    scanner.cfg = {"upstox": {"quote_batch_size": 100}, "market": {}, "scan": {"min_coverage": 0.95}}
    scanner.fetcher = Fetcher()
    scanner.notifiers = []
    scanner.fundamentals = Fundamentals()
    scanner.get_active_stocks = lambda limit=None: [stock]
    scanner.required_daily_candles = lambda: 30
    completed = pd.DataFrame([{
        "ts": pd.Timestamp("2026-10-07") + pd.Timedelta(days=i),
        "open": 10, "high": 12, "low": 9, "close": 11, "volume": 100,
    } for i in range(30)])
    scanner._load_cached_daily_frames = lambda _stocks, _db: {"AAA": completed}
    evaluated = []

    def evaluate(_stock, frame, provisional=False):
        evaluated.append((provisional, frame.copy()))
        signal_type = "provisional_intraday_confluence_buy" if provisional else "confluence_buy"
        return ({"signal_type": signal_type, "direction": "BUY", "price": frame.close.iloc[-1],
                 "confluence_score": 2, "total_signals": 3, "checks": []}, frame)

    scanner._scan_stock = evaluate
    delivered = []
    scanner._dispatch_alert = lambda payload, db, signal_id: delivered.append(payload) or True
    monkeypatch.setattr("src.scanner.get_session", FakeDB)
    monkeypatch.setattr("src.scanner.seed_legacy_deliveries", lambda *args: None)

    def fake_upsert_signal(**kwargs):
        detail = kwargs["indicator_detail"]
        return type("Saved", (), {
            "id": len(delivered) + 1, "indicator_detail": detail,
            "indicator_value": kwargs["indicator_value"], "chart_path": "chart.png",
        })()

    monkeypatch.setattr("src.scanner.upsert_signal", fake_upsert_signal)
    monkeypatch.setattr("charts.chart_generator.generate_signal_chart", lambda **kwargs: "chart.png")

    summary = scanner.run_scan(now=now)
    assert summary.complete
    assert [is_provisional for is_provisional, _ in evaluated] == [False, True]
    assert evaluated[0][1].ts.iloc[-1].date() == date(2026, 11, 5)
    assert evaluated[1][1].ts.iloc[-1].date() == date(2026, 11, 6)
    assert evaluated[1][1].close.iloc[-1] == 105  # session OHLC; not persisted to the DB
    assert [payload.provisional for payload in delivered] == [False, True]
    assert delivered[1].signal_type == "provisional_intraday_confluence_buy"
    assert delivered[1].format_signal_label().startswith("PROVISIONAL")
    assert delivered[1].signal_session_date == "2026-11-06"
