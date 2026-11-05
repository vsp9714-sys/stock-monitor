from datetime import datetime

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.db import OHLCVCache, upsert_ohlcv_rows
from src.fetcher.upstox import IncompleteQuoteBatch, UpstoxAPIError, UpstoxFetcher
from src.rate_limiter import RateLimiter
from src.scanner import Scanner


@pytest.fixture
def test_db_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'cache.db'}")
    from src.db import Base
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def test_rate_limiter_spaces_call_starts():
    now = [0.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    limiter = RateLimiter(0.5, monotonic=lambda: now[0], sleep=sleep)
    limiter.wait()
    limiter.wait()
    limiter.wait()
    assert sleeps == [0.5, 0.5]


def test_quote_fetcher_batches_and_maps_instrument_tokens(monkeypatch):
    fetcher = object.__new__(UpstoxFetcher)
    fetcher._quote_limiter = object()
    calls = []

    def fake_get_json(url, params, limiter):
        batch = params["instrument_key"].split(",")
        calls.append(batch)
        return {"data": {
            f"key_{i}": {
                "instrument_token": key,
                "last_price": 101.0, "prev_close_price": 99.0,
                "last_trade_time": 1_800_000_000_000,
                "ohlc": {"open": 1, "high": 2, "low": 0.5, "close": 1.5,
                         "volume": 42, "ts": 1_800_000_000_000},
            }
            for i, key in enumerate(batch)
        }}

    monkeypatch.setattr(fetcher, "_get_json", fake_get_json)
    keys = [f"NSE_EQ|INE{i:09d}" for i in range(205)]
    result = fetcher.get_full_market_quotes(keys, batch_size=100)
    assert [len(batch) for batch in calls] == [100, 100, 5]
    assert len(result) == 205
    assert result[keys[0]]["volume"] == 42.0
    assert result[keys[-1]]["last_price"] == 101.0
    assert result[keys[-1]]["prev_close_price"] == 99.0
    assert result[keys[-1]]["close"] == 1.5


def test_quote_fetcher_fails_closed_on_missing_key(monkeypatch):
    fetcher = object.__new__(UpstoxFetcher)
    fetcher._quote_limiter = object()
    monkeypatch.setattr(fetcher, "_get_json", lambda *args: {"data": {}})
    with pytest.raises(IncompleteQuoteBatch):
        fetcher.get_full_market_quotes(["NSE_EQ|INE000A01000"])


def test_quote_fetcher_can_return_available_quotes_and_skip_missing(monkeypatch):
    fetcher = object.__new__(UpstoxFetcher)
    fetcher._quote_limiter = object()
    available = "NSE_EQ|INE000A01000"
    missing = "NSE_EQ|INE000A01001"
    monkeypatch.setattr(fetcher, "_get_json", lambda *args: {"data": {
        "row": {"instrument_token": available, "last_price": 10,
                "prev_close_price": 9, "last_trade_time": 1_800_000_000_000,
                "ohlc": {"open": 1, "high": 2, "low": 0.5, "close": 1.5,
                         "volume": 42, "ts": 1_800_000_000_000}}
    }})
    result = fetcher.get_full_market_quotes([available, missing], allow_missing=True)
    assert list(result) == [available]


def test_upstox_retries_429_with_bounded_retry_after(monkeypatch):
    class Response:
        headers = {"Retry-After": "0"}

        def __init__(self, status_code):
            self.status_code = status_code

        def raise_for_status(self):
            if self.status_code >= 400:
                import requests
                raise requests.HTTPError("throttled")

        def json(self):
            return {"data": "ok"}

    fetcher = object.__new__(UpstoxFetcher)
    fetcher.auth = type("Auth", (), {"auth_headers": lambda self: {}})()
    fetcher._request_retries = 2
    calls = []
    monkeypatch.setattr("src.fetcher.upstox.requests.get", lambda *a, **k: (
        calls.append(1) or Response(429 if len(calls) == 1 else 200)
    ))
    monkeypatch.setattr(UpstoxFetcher, "_retry_delay", staticmethod(lambda response, attempt: 0))

    class Limiter:
        def __init__(self):
            self.waits = 0
            self.defers = []

        def wait(self):
            self.waits += 1

        def defer(self, seconds):
            self.defers.append(seconds)

    limiter = Limiter()
    assert fetcher._get_json("https://example.test", {}, limiter) == {"data": "ok"}
    assert len(calls) == limiter.waits == 2
    assert limiter.defers == [0]


def test_upstox_does_not_retry_permanent_4xx(monkeypatch):
    class Response:
        status_code = 401
        headers = {}

        def raise_for_status(self):
            import requests
            raise requests.HTTPError("unauthorized")

    fetcher = object.__new__(UpstoxFetcher)
    fetcher.auth = type("Auth", (), {"auth_headers": lambda self: {}})()
    fetcher._request_retries = 3
    calls = []
    monkeypatch.setattr("src.fetcher.upstox.requests.get", lambda *a, **k: calls.append(1) or Response())

    class Limiter:
        def wait(self):
            pass

        def defer(self, seconds):
            raise AssertionError("permanent 4xx must not be retried")

    with pytest.raises(UpstoxAPIError):
        fetcher._get_json("https://example.test", {}, Limiter())
    assert len(calls) == 1


def test_ohlcv_upsert_is_idempotent_and_updates_latest(test_db_session):
    row = {"symbol": "ABC", "interval": "1d", "ts": datetime(2026, 1, 1),
           "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10}
    upsert_ohlcv_rows(test_db_session, [row])
    upsert_ohlcv_rows(test_db_session, [{**row, "close": 1.75, "volume": 20}])
    cached = test_db_session.query(OHLCVCache).one()
    assert cached.close == 1.75
    assert cached.volume == 20


def test_cache_warmup_resumes_completed_symbols(test_db_session, monkeypatch):
    stocks = [
        type("StockRef", (), {"symbol": symbol, "instrument_key": f"NSE_EQ|{symbol}"})()
        for symbol in ("AAA", "BBB")
    ]

    class Fetcher:
        calls = []

        def get_daily_candles(self, instrument_key, days):
            self.calls.append(instrument_key)
            return pd.DataFrame([{
                "ts": pd.Timestamp("2026-01-01") + pd.Timedelta(days=i),
                "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10,
            } for i in range(3)])

    fetcher = Fetcher()
    scanner = object.__new__(Scanner)
    scanner.fetcher = fetcher
    scanner.get_active_stocks = lambda limit=None: stocks
    scanner.required_daily_candles = lambda: 3
    monkeypatch.setattr("src.scanner.get_session", lambda: test_db_session)

    first = scanner.warm_candle_cache(checkpoint_symbols=1)
    assert first["warmed"] == 2 and first["failed"] == 0
    assert len(test_db_session.query(OHLCVCache).all()) == 6
    second = scanner.warm_candle_cache()
    assert second["already_complete"] == 2 and second["warmed"] == 0
    assert len(fetcher.calls) == 2


def test_scan_stops_before_alerts_when_quote_batch_request_fails(monkeypatch):
    stock = type("StockRef", (), {
        "symbol": "ABC", "instrument_key": "NSE_EQ|INE000A01000",
    })()

    class FakeDB:
        def close(self):
            pass

    class Fetcher:
        def get_full_market_quotes(self, *args, **kwargs):
            raise IncompleteQuoteBatch("missing key")

    class Notifier:
        calls = 0

        def delivery_targets(self):
            return ["telegram:individual"]

        def notify_target(self, *args, **kwargs):
            self.calls += 1

    scanner = object.__new__(Scanner)
    scanner.cfg = {"upstox": {"quote_batch_size": 100}}
    scanner.fetcher = Fetcher()
    scanner.notifiers = [Notifier()]
    scanner.get_active_stocks = lambda limit=None: [stock]
    scanner.required_daily_candles = lambda: 30
    scanner._load_cached_daily_frames = lambda stocks, db: {
        "ABC": pd.DataFrame([{
            "ts": pd.Timestamp("2026-01-01") + pd.Timedelta(days=i),
            "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 10,
        } for i in range(30)])
    }
    monkeypatch.setattr("src.scanner.get_session", FakeDB)

    result = scanner.run_scan()
    assert result.complete is False
    assert result.alerts_dispatched == 0
    assert scanner.notifiers[0].calls == 0


def test_scan_skips_missing_symbol_but_dispatches_for_quoted_symbols(monkeypatch):
    stocks = [
        type("StockRef", (), {"symbol": symbol, "instrument_key": f"NSE_EQ|{symbol}",
                              "company_name": symbol, "isin": "INE000A01000"})()
        for symbol in ("AAA", "GNRL")
    ]
    from datetime import timezone
    today_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

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
        def get_full_market_quotes(self, keys, **kwargs):
            return {"NSE_EQ|AAA": {"open": 100, "high": 120, "low": 90,
                                   "close": 105, "volume": 100, "ts": today_ms,
                                   "last_price": 112, "prev_close_price": 95,
                                   "last_trade_time": today_ms}}

    class Fundamentals:
        def get_fundamentals(self, *args):
            return {}

    scanner = object.__new__(Scanner)
    scanner.cfg = {"upstox": {"quote_batch_size": 100}}
    scanner.fetcher = Fetcher()
    scanner.notifiers = [type("Notifier", (), {"delivery_targets": lambda self: []})()]
    scanner.fundamentals = Fundamentals()
    scanner.get_active_stocks = lambda limit=None: stocks
    scanner.required_daily_candles = lambda: 30
    frame = pd.DataFrame([{
        "ts": pd.Timestamp("2026-01-01") + pd.Timedelta(days=i),
        "open": 10, "high": 12, "low": 9, "close": 11, "volume": 100,
    } for i in range(30)])
    scanner._load_cached_daily_frames = lambda _stocks, _db: {"AAA": frame, "GNRL": frame}
    result_data = {"signal_type": "confluence", "direction": "BUY", "price": 11,
                   "confluence_score": 2, "total_signals": 6, "checks": []}
    scanner._scan_stock = lambda stock, daily: (result_data, daily)
    sent = []
    scanner._dispatch_alert = lambda payload, db, signal_id: sent.append((payload.symbol, payload.price)) or True
    monkeypatch.setattr("src.scanner.get_session", FakeDB)
    monkeypatch.setattr("src.scanner.upsert_ohlcv_rows", lambda *args: None)
    monkeypatch.setattr("src.scanner.seed_legacy_deliveries", lambda *args: None)
    monkeypatch.setattr("src.scanner.upsert_signal", lambda **kwargs: type("Saved", (), {
        "id": 1, "indicator_detail": '{"checks": [], "total_signals": 6}',
        "indicator_value": 2, "chart_path": "chart.png"})())
    monkeypatch.setattr("charts.chart_generator.generate_signal_chart", lambda **kwargs: "chart.png")

    result = scanner.run_scan()
    assert result.complete is False
    assert result.evaluated_count == 1
    assert result.failed_count == 1
    assert result.alerts_dispatched == 1
    assert sent == [("AAA", 112)]
