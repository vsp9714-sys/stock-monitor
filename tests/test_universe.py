"""WP17: deterministic universe selection and provider failure boundaries."""
import csv
import io
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.db import Base, UniverseDataSnapshot, UniverseRefreshAttempt, UniverseScanSnapshot
from src.universe import (
    NSE_FNO_URL,
    NIFTY500_URL,
    UniverseSelectionError,
    UniverseSelector,
    parse_fno_lots_csv,
    parse_nifty500_csv,
)


def _csv(header, rows):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(header)
    writer.writerows(rows)
    return out.getvalue()


def _nifty_csv(count=450):
    return _csv(["Company Name", "Industry", "Symbol", "Series", "ISIN Code"],
                [[f"Company {i}", "Industry", f"S{i:04}", "EQ", f"IN{i:010}"] for i in range(count)])


def _stocks(count=450, extra=()):
    return [SimpleNamespace(symbol=f"S{i:04}", series="EQ", last_updated=datetime(2026, 11, 6))
            for i in range(count)] + list(extra)


@pytest.fixture
def selector_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'universe.db'}")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


def test_nifty500_selection_and_daily_refresh_are_deterministic(selector_db):
    calls = []
    selector = UniverseSelector(
        {"market": {"trading_days": [0, 1, 2, 3, 4]}, "universe": {"mode": "nifty500"}},
        selector_db,
        get_source=lambda url: calls.append(url) or _nifty_csv(),
    )
    now = datetime(2026, 11, 6, 8, tzinfo=timezone.utc)
    first = selector.resolve(_stocks(500), now=now)
    second = selector.resolve(_stocks(500), now=now)

    assert len(first.symbols) == len(second.symbols) == 450
    assert [stock.symbol for stock in first.symbols] == [stock.symbol for stock in second.symbols]
    assert calls == [NIFTY500_URL]
    with selector_db() as session:
        assert session.query(UniverseDataSnapshot).count() == 1
        assert session.query(UniverseRefreshAttempt).filter_by(status="success").count() == 1
        assert session.query(UniverseScanSnapshot).count() == 1


def test_nifty_csv_rejects_blank_duplicate_and_implausible_data():
    with pytest.raises(ValueError, match="Malformed"):
        parse_nifty500_csv(_csv(["Symbol"], [[""], ["ABC"]]), 1, 10)
    with pytest.raises(ValueError, match="duplicate"):
        parse_nifty500_csv(_csv(["Symbol"], [["ABC"], [" abc "]]), 1, 10)
    with pytest.raises(ValueError, match="outside"):
        parse_nifty500_csv(_csv(["Symbol"], [["ABC"]]), 2, 10)
    with pytest.raises(ValueError, match="missing the Symbol"):
        parse_nifty500_csv(_csv(["ticker"], [["ABC"]]), 1, 10)


def test_invalid_source_uses_stale_lkg_and_no_cache_fails_closed(selector_db):
    yesterday = date(2026, 11, 5)
    with selector_db() as session:
        session.add(UniverseDataSnapshot(
            source_key="nifty500", snapshot_date=yesterday, source=NIFTY500_URL,
            retrieved_at=datetime(2026, 11, 5, 12, tzinfo=timezone.utc),
            symbols_json='["S0000", "S0001"]', item_count=2,
        ))
        session.commit()
    failure_calls = []
    selector = UniverseSelector(
        {"universe": {"mode": "nifty500", "nifty500_min_count": 1, "nifty500_max_count": 10}},
        selector_db,
        get_source=lambda _url: failure_calls.append(_url) or (_ for _ in ()).throw(RuntimeError("offline")),
    )
    result = selector.resolve(_stocks(3), now=datetime(2026, 11, 6, 8, tzinfo=timezone.utc))
    assert [stock.symbol for stock in result.symbols] == ["S0000", "S0001"]
    assert result.metadata["source_metadata"]["nifty500"]["stale"] is True
    assert "offline" in result.metadata["source_metadata"]["nifty500"]["source_error"]
    selector.resolve(_stocks(3), now=datetime(2026, 11, 6, 9, tzinfo=timezone.utc))
    assert failure_calls == [NIFTY500_URL]

    empty_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(empty_engine)
    empty_factory = sessionmaker(bind=empty_engine)
    no_cache = UniverseSelector(
        {"universe": {"mode": "nifty500"}}, empty_factory,
        get_source=lambda _url: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    with pytest.raises(UniverseSelectionError, match="No valid cached"):
        no_cache.resolve(_stocks(450), now=datetime(2026, 11, 6, 8, tzinfo=timezone.utc))
    empty_engine.dispose()


def test_composed_mode_is_union_deduplicated_and_reports_fno_failure(selector_db):
    rows = _stocks(450, [SimpleNamespace(symbol="CUSTOM1", series="EQ", last_updated=datetime(2026, 11, 6)),
                         SimpleNamespace(symbol="FNO1", series="EQ", last_updated=datetime(2026, 11, 6))])
    fno_csv = _csv(["UNDERLYING", "SYMBOL", "NOV-26"],
                   [["Company", "S0000", "1"], ["Company", "FNO1", "1"],
                    ["NIFTY 50", "NIFTY", "65"]])
    selector = UniverseSelector(
        {"universe": {"mode": "composed", "custom_symbols": ["CUSTOM1", "S0000"], "include_fno": True}},
        selector_db,
        get_source=lambda url: _nifty_csv() if url == NIFTY500_URL else fno_csv,
    )
    result = selector.resolve(rows, now=datetime(2026, 11, 6, 8, tzinfo=timezone.utc))
    symbols = [stock.symbol for stock in result.symbols]
    assert len(symbols) == 452
    assert len(symbols) == len(set(symbols))
    assert {"CUSTOM1", "FNO1", "S0000"}.issubset(symbols)
    assert result.metadata["fno_count"] == 2

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    isolated_factory = sessionmaker(bind=engine)
    selector2 = UniverseSelector(
        {"universe": {"mode": "composed", "custom_symbols": ["CUSTOM1"], "include_fno": True}},
        isolated_factory,
        get_source=lambda url: _nifty_csv() if url == NIFTY500_URL else (_ for _ in ()).throw(RuntimeError("F&O offline")),
    )
    result2 = selector2.resolve(rows, now=datetime(2026, 11, 6, 8, tzinfo=timezone.utc))
    assert result2.metadata["source_metadata"]["fno_eligible"]["available"] is False
    assert "F&O offline" in result2.metadata["source_metadata"]["fno_eligible"]["source_error"]
    assert len(result2.symbols) == 451
    engine.dispose()


def test_custom_series_default_and_all_nse_backward_compatibility(selector_db):
    extra = [SimpleNamespace(symbol="SMALLCO", series="SM", last_updated=datetime(2026, 11, 6))]
    all_nse = UniverseSelector({"universe": {"mode": "all_nse"}}, selector_db)
    assert len(all_nse.resolve(_stocks(2, extra), now=datetime(2026, 11, 6, 8, tzinfo=timezone.utc)).symbols) == 3

    custom = UniverseSelector({"universe": {"mode": "custom", "custom_symbols": ["smallco"]}}, selector_db)
    with pytest.raises(UniverseSelectionError, match="no validated instruments"):
        custom.resolve(_stocks(2, extra), now=datetime(2026, 11, 6, 8, tzinfo=timezone.utc))

    opted_in = UniverseSelector({"universe": {"mode": "custom", "custom_symbols": ["smallco"],
                                                  "allowed_series": ["EQ", "SM"]}}, selector_db)
    assert [stock.symbol for stock in opted_in.resolve(
        _stocks(2, extra), now=datetime(2026, 11, 6, 8, tzinfo=timezone.utc)
    ).symbols] == ["SMALLCO"]


def test_invalid_mode_fails_closed(selector_db):
    selector = UniverseSelector({"universe": {"mode": "nifty50"}}, selector_db)
    with pytest.raises(UniverseSelectionError, match="Unsupported universe mode"):
        selector.resolve(_stocks(1), now=datetime(2026, 11, 6, 8, tzinfo=timezone.utc))


def test_fno_lot_file_parser_excludes_index_rows_by_instrument_intersection():
    raw = _csv(["UNDERLYING", "SYMBOL", "NOV-26", "DEC-26"],
               [["NIFTY 50", "NIFTY", "65", "65"], ["Company", "ABC", "1", "1"]])
    assert parse_fno_lots_csv(raw) == ["ABC", "NIFTY"]


def test_failed_universe_selection_marks_scan_incomplete_without_quotes():
    from src.scanner import Scanner

    scanner = object.__new__(Scanner)
    scanner.universe_cfg = {"mode": "nifty500"}
    scanner.get_active_stocks = lambda **_kwargs: (_ for _ in ()).throw(
        UniverseSelectionError("source failed; no LKG")
    )
    summary = scanner.run_scan()
    assert summary.complete is False
    assert summary.universe_size == 0
    assert summary.alerts_dispatched == 0
    assert summary.universe_metadata["selection_complete"] is False
