"""Deterministic stock-universe selection with validated, auditable sources."""
import csv
import io
import json
import logging
import re
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable

import requests

from src.db import UniverseDataSnapshot, UniverseRefreshAttempt, UniverseScanSnapshot

logger = logging.getLogger(__name__)
_SOURCE_REFRESH_LOCK = threading.Lock()

NIFTY500_URL = "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv"
NSE_FNO_URL = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"
_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9&.\-]*$")


class UniverseSelectionError(RuntimeError):
    """Raised when a requested universe cannot be safely resolved."""


@dataclass
class UniverseResult:
    symbols: list[str]
    metadata: dict


def normalize_symbol(value: str) -> str:
    symbol = str(value or "").strip().upper()
    if not symbol or not _SYMBOL.fullmatch(symbol):
        raise ValueError(f"Malformed symbol: {value!r}")
    return symbol


def _validated_symbols(values: Iterable[str], minimum: int, maximum: int) -> list[str]:
    symbols = [normalize_symbol(value) for value in values]
    if len(symbols) != len(set(symbols)):
        raise ValueError("Source contains duplicate normalized symbols")
    if not minimum <= len(symbols) <= maximum:
        raise ValueError(f"Source count {len(symbols)} outside configured range {minimum}..{maximum}")
    return sorted(symbols)


def parse_nifty500_csv(content: bytes | str, minimum: int = 450, maximum: int = 550) -> list[str]:
    stream = io.StringIO(content.decode("utf-8-sig") if isinstance(content, bytes) else content.lstrip("\ufeff"))
    reader = csv.DictReader(stream)
    if not reader.fieldnames:
        raise ValueError("Nifty 500 CSV has no header")
    headers = {name.strip().lower(): name for name in reader.fieldnames if name}
    symbol_header = headers.get("symbol")
    if symbol_header is None:
        raise ValueError("Nifty 500 CSV is missing the Symbol column")
    rows = list(reader)
    return _validated_symbols((row.get(symbol_header, "") for row in rows), minimum, maximum)


def parse_fno_lots_csv(content: bytes | str) -> list[str]:
    """Parse NSE's official market-lot file into current stock F&O underlyings."""
    stream = io.StringIO(content.decode("utf-8-sig") if isinstance(content, bytes) else content.lstrip("\ufeff"))
    reader = csv.DictReader(stream)
    if not reader.fieldnames:
        raise ValueError("NSE F&O market-lot CSV has no header")
    headers = {name.strip().upper(): name for name in reader.fieldnames if name}
    symbol_header = headers.get("SYMBOL")
    if symbol_header is None:
        raise ValueError("NSE F&O market-lot CSV is missing the SYMBOL column")
    lot_headers = [name for key, name in headers.items() if key not in {"UNDERLYING", "SYMBOL"}]
    if not lot_headers:
        raise ValueError("NSE F&O market-lot CSV has no expiry lot-size columns")
    symbols = []
    for row in reader:
        symbol = (row.get(symbol_header) or "").strip()
        if not symbol:
            continue
        # Index contracts (NIFTY, BANKNIFTY, etc.) are intentionally excluded;
        # intersection with the verified cash-equity master occurs downstream.
        if any((row.get(column) or "").strip() for column in lot_headers):
            symbols.append(normalize_symbol(symbol))
    if not symbols:
        raise ValueError("NSE F&O market-lot source contained no eligible underlyings")
    if len(symbols) != len(set(symbols)):
        raise ValueError("NSE F&O source contains duplicate normalized symbols")
    return sorted(symbols)


class UniverseSelector:
    """Resolve configured membership; network/source failures never widen a set."""

    def __init__(self, cfg: dict, session_factory, *, get_source=None):
        self.cfg = cfg
        self.session_factory = session_factory
        self.get_source = get_source or self._get_source
        self.settings = cfg.get("universe", {})

    def get_fno_underlyings(self, today: date) -> tuple[set[str], dict]:
        """Load authoritative daily NSE stock F&O eligibility for optional OI context."""
        try:
            symbols, metadata = self._load_source(
                "fno_eligible", today, NSE_FNO_URL, parse_fno_lots_csv, allow_refresh=True
            )
            return set(symbols), metadata
        except UniverseSelectionError as exc:
            return set(), {"available": False, "source": NSE_FNO_URL, "source_error": str(exc)}

    @staticmethod
    def _get_source(url: str) -> bytes:
        response = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "text/csv,*/*"},
            timeout=30,
        )
        response.raise_for_status()
        return response.content

    def _latest_valid(self, source_key: str):
        session = self.session_factory()
        try:
            return session.query(UniverseDataSnapshot).filter_by(source_key=source_key).order_by(
                UniverseDataSnapshot.snapshot_date.desc(), UniverseDataSnapshot.retrieved_at.desc()
            ).first()
        finally:
            session.close()

    def _snapshot_for_day(self, source_key: str, today: date):
        session = self.session_factory()
        try:
            return session.query(UniverseDataSnapshot).filter_by(
                source_key=source_key, snapshot_date=today
            ).order_by(UniverseDataSnapshot.retrieved_at.desc()).first()
        finally:
            session.close()

    def _attempt_for_day(self, source_key: str, today: date):
        session = self.session_factory()
        try:
            return session.query(UniverseRefreshAttempt).filter_by(
                source_key=source_key, attempt_date=today
            ).first()
        finally:
            session.close()

    def _claim_attempt(self, source_key: str, today: date) -> bool:
        session = self.session_factory()
        try:
            session.add(UniverseRefreshAttempt(
                source_key=source_key, attempt_date=today,
                attempted_at=datetime.now(timezone.utc), status="pending",
            ))
            session.commit()
            return True
        except Exception:
            # A unique source/day claim prevents duplicate HTTP fetches even
            # if another process races this selector.
            session.rollback()
            return False
        finally:
            session.close()

    def _finish_attempt(self, source_key: str, today: date, status: str, error: str | None):
        session = self.session_factory()
        try:
            attempt = session.query(UniverseRefreshAttempt).filter_by(
                source_key=source_key, attempt_date=today
            ).first()
            if attempt is not None:
                attempt.status = status
                attempt.source_error = error[:1000] if error else None
                session.commit()
        finally:
            session.close()

    def _record_data(self, source_key: str, today: date, source: str, symbols: list[str]):
        session = self.session_factory()
        try:
            session.add(UniverseDataSnapshot(
                source_key=source_key, snapshot_date=today, source=source,
                retrieved_at=datetime.now(timezone.utc),
                symbols_json=json.dumps(symbols), item_count=len(symbols),
            ))
            session.commit()
        finally:
            session.close()

    def _load_source(self, source_key: str, today: date, url: str, parser, *, allow_refresh: bool):
        # The scheduler may have startup and timed refresh paths; serialize
        # in-process source checks so one date can never trigger duplicate fetches.
        with _SOURCE_REFRESH_LOCK:
            return self._load_source_locked(source_key, today, url, parser, allow_refresh=allow_refresh)

    def _load_source_locked(self, source_key: str, today: date, url: str, parser, *, allow_refresh: bool):
        """Attempt source once/day; on failure resolve to last-known-good as stale."""
        attempt = self._attempt_for_day(source_key, today)
        current = self._snapshot_for_day(source_key, today)
        error = attempt.source_error if attempt and attempt.status != "success" else None
        if current:
            return json.loads(current.symbols_json), {
                "source": current.source, "retrieved_at": current.retrieved_at.isoformat(),
                "snapshot_date": current.snapshot_date.isoformat(), "stale": False,
                "available": True, "source_error": None,
            }
        if attempt is None and allow_refresh:
            claimed = self._claim_attempt(source_key, today)
            if not claimed:
                attempt = self._attempt_for_day(source_key, today)
                error = attempt.source_error if attempt and attempt.source_error else "refresh already attempted or in progress today"
            else:
                try:
                    content = self.get_source(url)
                    if source_key == "nifty500":
                        symbols = parser(
                            content,
                            int(self.settings.get("nifty500_min_count", 450)),
                            int(self.settings.get("nifty500_max_count", 550)),
                        )
                    else:
                        symbols = parser(content)
                    self._record_data(source_key, today, url, symbols)
                    self._finish_attempt(source_key, today, "success", None)
                    snapshot = self._snapshot_for_day(source_key, today)
                    return symbols, {
                        "source": url, "retrieved_at": snapshot.retrieved_at.isoformat(),
                        "snapshot_date": today.isoformat(), "stale": False, "available": True,
                        "source_error": None,
                    }
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    logger.warning("Universe source %s refresh failed: %s", source_key, error)
                    self._finish_attempt(source_key, today, "failed", error)
        latest = self._latest_valid(source_key)
        if latest is None:
            raise UniverseSelectionError(
                f"No valid cached {source_key} membership and source refresh is unavailable: "
                f"{error or 'today is not a configured trading day'}"
            )
        if not allow_refresh and not error:
            error = "refresh deferred until a configured trading day"
        if attempt and attempt.status == "pending" and not error:
            error = "refresh is already in progress today"
        return json.loads(latest.symbols_json), {
            "source": latest.source, "retrieved_at": latest.retrieved_at.isoformat(),
            "snapshot_date": latest.snapshot_date.isoformat(), "stale": True,
            "available": True,
            "stale_age_days": (today - latest.snapshot_date).days,
            "source_error": error or "refresh already failed today",
        }

    def _record_scan_snapshot(self, today: date, mode: str, metadata: dict, symbols: list[str]):
        session = self.session_factory()
        try:
            metadata_json = json.dumps(metadata, sort_keys=True)
            symbols_json = json.dumps(symbols)
            snapshot = session.query(UniverseScanSnapshot).filter_by(
                snapshot_date=today, mode=mode,
                metadata_json=metadata_json, symbols_json=symbols_json,
            ).first()
            # The selected set is deterministic within a day; avoid a SQLite
            # write on every 15-minute scan after the first daily snapshot.
            if snapshot is not None:
                return
            snapshot = UniverseScanSnapshot(snapshot_date=today, mode=mode)
            session.add(snapshot)
            snapshot.created_at = datetime.now(timezone.utc)
            snapshot.metadata_json = metadata_json
            snapshot.symbols_json = symbols_json
            session.commit()
        finally:
            session.close()

    def resolve(self, stocks: list, *, now: datetime | None = None) -> UniverseResult:
        current = now or datetime.now(timezone.utc)
        today = current.date()
        trading_days = set(self.cfg.get("market", {}).get("trading_days", [0, 1, 2, 3, 4]))
        allow_refresh = current.weekday() in trading_days
        mode = str(self.settings.get("mode", "all_nse")).strip().lower()
        supported = {"all_nse", "nifty500", "custom", "composed"}
        if mode not in supported:
            raise UniverseSelectionError(f"Unsupported universe mode {mode!r}; choose {sorted(supported)}")

        by_symbol = {stock.symbol: stock for stock in stocks}
        for stock in stocks:
            try:
                normalize_symbol(stock.symbol)
            except ValueError as exc:
                raise UniverseSelectionError(f"Instrument master contains invalid symbol {stock.symbol!r}") from exc

        allowed_series = self.settings.get("allowed_series")
        # Preserve the legacy all_nse set when no new series filter is configured.
        if allowed_series is None:
            allowed = None if mode == "all_nse" else {"EQ"}
        else:
            allowed = {str(value).strip().upper() for value in allowed_series}
        if allowed is not None and (not allowed or not allowed.issubset({"EQ", "BE", "BZ", "SM", "ST", "SZ"})):
            raise UniverseSelectionError("universe.allowed_series must contain recognized NSE share series")

        source_meta = {}
        constituent_symbols: list[str] = []
        fno_symbols: list[str] = []
        custom = []
        source_errors = []

        if mode in {"nifty500", "composed"}:
            constituent_symbols, source_meta["nifty500"] = self._load_source(
                "nifty500", today, NIFTY500_URL, parse_nifty500_csv, allow_refresh=allow_refresh
            )
            if source_meta["nifty500"].get("source_error"):
                source_errors.append(source_meta["nifty500"]["source_error"])
        if mode in {"custom", "composed"}:
            raw_custom = self.settings.get("custom_symbols", [])
            if not isinstance(raw_custom, list) or (mode == "custom" and not raw_custom):
                raise UniverseSelectionError("universe.custom_symbols must be a non-empty list for custom mode")
            try:
                custom = sorted({normalize_symbol(symbol) for symbol in raw_custom})
            except ValueError as exc:
                raise UniverseSelectionError(str(exc)) from exc
        if mode == "composed" and bool(self.settings.get("include_fno", False)):
            try:
                fno_symbols, source_meta["fno_eligible"] = self._load_source(
                    "fno_eligible", today, NSE_FNO_URL, parse_fno_lots_csv, allow_refresh=allow_refresh
                )
            except UniverseSelectionError as exc:
                # F&O is an explicitly optional composed addition. Keep the
                # Nifty/custom core, expose the omission, and never broaden it.
                source_meta["fno_eligible"] = {
                    "enabled": True, "available": False, "stale": False,
                    "source": NSE_FNO_URL, "retrieved_at": None,
                    "source_error": str(exc),
                }
            # The official lot file includes index contracts as well as stock
            # underlyings. Only verified cash instruments are stock candidates.
            fno_symbols = [symbol for symbol in fno_symbols if symbol in by_symbol]
            if source_meta["fno_eligible"].get("source_error"):
                source_errors.append(source_meta["fno_eligible"]["source_error"])
        elif mode == "composed":
            source_meta["fno_eligible"] = {"enabled": False, "stale": False, "source_error": None}

        if mode == "all_nse":
            candidate_set = set(by_symbol)
        elif mode == "nifty500":
            candidate_set = set(constituent_symbols)
        elif mode == "custom":
            candidate_set = set(custom)
        else:
            candidate_set = set(constituent_symbols) | set(custom) | set(fno_symbols)

        mapped = candidate_set & set(by_symbol)
        unmatched = sorted(candidate_set - set(by_symbol))
        series_excluded = sorted(
            symbol for symbol in mapped
            if allowed is not None and str(by_symbol[symbol].series or "").upper() not in allowed
        )
        selected = sorted(mapped - set(series_excluded))
        if not selected:
            raise UniverseSelectionError(f"Universe mode {mode!r} resolved to no validated instruments")

        master_retrieved = max(
            (stock.last_updated for stock in stocks if stock.last_updated is not None),
            default=None,
        )
        master_age_days = (today - master_retrieved.date()).days if master_retrieved else None
        metadata = {
            "mode": mode,
            "snapshot_date": today.isoformat(),
            "instrument_master": {
                "source": "Upstox instrument master cross-checked against NSE equity masters",
                "retrieved_at": master_retrieved.isoformat() if master_retrieved else None,
                "age_days": master_age_days,
                "stale": master_age_days is None or master_age_days > 1,
            },
            "source_metadata": source_meta,
            "constituent_count": len(set(constituent_symbols) & candidate_set),
            "custom_count": len(set(custom) & candidate_set),
            "fno_count": len(set(fno_symbols) & candidate_set),
            "candidate_unique_count": len(candidate_set),
            "selected_count": len(selected),
            "final_tradable_count": len(selected),
            "series_filter": sorted(allowed) if allowed is not None else "legacy_all_share_series",
            "series_exclusion_count": len(series_excluded),
            "series_excluded_symbols": series_excluded,
            "unmapped_symbols_count": len(unmatched),
            "unmapped_symbols": unmatched,
            "source_errors": source_errors,
            "selection_complete": True,
            "liquidity_filter_applied": False,
            "liquidity_filter_note": "WP10 pending; final_tradable_count is pre-liquidity selected count",
        }
        self._record_scan_snapshot(today, mode, metadata, selected)
        return UniverseResult([by_symbol[symbol] for symbol in selected], metadata)
