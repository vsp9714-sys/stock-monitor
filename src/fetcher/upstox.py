"""
Upstox data fetcher — handles:
1. OAuth2 token management (refresh flow)
2. Instrument master download (all NSE/BSE EQ stocks)
3. Historical OHLCV candles for signal computation
4. Real-time LTP (Last Traded Price) for price confirmation

Upstox API v2 docs: https://upstox.com/developer/api-documentation/
"""
import json
import io
import logging
import math
import random
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import pandas as pd
import requests
from zoneinfo import ZoneInfo

from src.config_loader import get_config
from src.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

BASE_URL = "https://api.upstox.com/v2"
BASE_URL_V3 = "https://api.upstox.com/v3"
TOKEN_CACHE = Path("data/upstox_token.json")
IST = ZoneInfo("Asia/Kolkata")


class UpstoxAPIError(RuntimeError):
    """Provider request failed after bounded retries."""

    def __init__(self, message: str, *, status_code: Optional[int] = None, error_code: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


class IncompleteQuoteBatch(UpstoxAPIError):
    """Quote API response did not contain every requested instrument."""

    def __init__(self, missing_keys: set[str]):
        self.missing_keys = frozenset(missing_keys)
        super().__init__(
            f"Upstox quote response omitted {len(self.missing_keys)} requested instruments"
        )


class UpstoxAuth:
    """Manages Upstox OAuth2 token lifecycle."""

    def __init__(self, cfg: dict):
        self.api_key = cfg["upstox"]["api_key"]
        self.api_secret = cfg["upstox"]["api_secret"]
        self.redirect_uri = cfg["upstox"].get("redirect_uri", "http://localhost:8080/callback")
        self._access_token: Optional[str] = cfg["upstox"].get("access_token", "")
        self._refresh_token: Optional[str] = cfg["upstox"].get("refresh_token", "")
        self._token_expiry: Optional[datetime] = None

    def get_login_url(self) -> str:
        """Returns the URL the user must visit to authorize the app (first-time setup)."""
        params = urlencode({
            "client_id": self.api_key,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
        })
        return f"https://api.upstox.com/v2/login/authorization/dialog?{params}"

    def exchange_code(self, auth_code: str) -> dict:
        """Exchange auth code for access + refresh tokens."""
        resp = requests.post(
            f"{BASE_URL}/login/authorization/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "code": auth_code,
                "client_id": self.api_key,
                "client_secret": self.api_secret,
                "redirect_uri": self.redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        self._access_token = data["access_token"]
        self._refresh_token = data.get("refresh_token", "")
        self._save_token(data)
        return data

    def _save_token(self, data: dict) -> None:
        TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
        data["saved_at"] = datetime.now(timezone.utc).isoformat()
        with open(TOKEN_CACHE, "w") as f:
            json.dump(data, f, indent=2)
        logger.info("Upstox token saved to cache.")

    def _load_cached_token(self) -> bool:
        """Load token from cache if available."""
        if not TOKEN_CACHE.exists():
            return False
        with open(TOKEN_CACHE) as f:
            data = json.load(f)
        saved_at = datetime.fromisoformat(data.get("saved_at", "2000-01-01"))
        if saved_at.tzinfo is None:
            saved_at = saved_at.replace(tzinfo=timezone.utc)
        # Upstox access tokens are valid for 1 day
        if datetime.now(timezone.utc) - saved_at < timedelta(hours=23):
            self._access_token = data.get("access_token", "")
            self._refresh_token = data.get("refresh_token", "")
            logger.info("Loaded Upstox token from cache.")
            return True
        return False

    def get_access_token(self) -> str:
        """Return a valid access token, loading from cache if needed."""
        if self._access_token:
            return self._access_token
        if self._load_cached_token():
            return self._access_token
        raise RuntimeError(
            "Upstox access token not available. "
            "Run: python -m src.fetcher.upstox_auth_setup to complete OAuth flow."
        )

    def auth_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.get_access_token()}",
            "Accept": "application/json",
        }


class UpstoxFetcher:
    """Fetches market data from Upstox API."""

    # Instrument CSV URL (updated regularly by Upstox)
    INSTRUMENT_CSV_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.csv.gz"
    INSTRUMENT_CSV_BSE_URL = "https://assets.upstox.com/market-quote/instruments/exchange/BSE.csv.gz"
    # Upstox classifies some listed bonds as EQUITY. Cross-check NSE instruments
    # against its common-equity master, whose ISIN/series fields distinguish shares.
    NSE_EQUITY_MASTER_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
    NSE_SME_EQUITY_MASTER_URL = "https://nsearchives.nseindia.com/emerge/corporates/content/SME_EQUITY_L.csv"
    NSE_SHARE_SERIES = {"EQ", "BE", "BZ", "SM", "ST", "SZ"}
    MIN_NSE_MASTER_EQUITIES = 1000
    MIN_MATCHED_NSE_EQUITIES = 1000

    def __init__(self):
        self.cfg = get_config()
        self.auth = UpstoxAuth(self.cfg)
        self._instrument_map: dict[str, str] = {}  # symbol → instrument_key
        upstox_cfg = self.cfg.get("upstox", {})
        self._historical_limiter = RateLimiter(
            upstox_cfg.get("historical_min_interval_seconds", 1.0)
        )
        self._quote_limiter = RateLimiter(
            upstox_cfg.get("quote_min_interval_seconds", 0.1)
        )
        self._request_retries = int(upstox_cfg.get("request_max_retries", 3))

    @staticmethod
    def _retry_delay(response, attempt: int) -> float:
        """Return a bounded Retry-After/exponential delay, with small jitter."""
        retry_after = response.headers.get("Retry-After") if response is not None else None
        delay = 0.0
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                try:
                    delay = max(0.0, (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds())
                except (TypeError, ValueError, OverflowError):
                    delay = 0.0
        if delay <= 0:
            delay = min(30.0, 2.0 ** attempt)
        return min(60.0, delay + random.uniform(0.0, min(0.5, delay * 0.1)))

    def _get_json(self, url: str, params: dict, limiter: RateLimiter) -> dict:
        """GET a provider endpoint with shared pacing and bounded transient retry."""
        last_error = None
        for attempt in range(self._request_retries + 1):
            limiter.wait()
            try:
                response = requests.get(
                    url, headers=self.auth.auth_headers(), params=params, timeout=30
                )
                if response.status_code == 429 or response.status_code >= 500:
                    last_error = UpstoxAPIError(
                        f"Upstox returned HTTP {response.status_code} for {url}",
                        status_code=response.status_code,
                    )
                    if attempt >= self._request_retries:
                        break
                    delay = self._retry_delay(response, attempt)
                    limiter.defer(delay)
                    logger.warning(
                        "Upstox HTTP %s; retry %s/%s after %.1fs",
                        response.status_code, attempt + 1, self._request_retries, delay,
                    )
                    continue
                try:
                    response.raise_for_status()
                except requests.HTTPError as exc:
                    # Retry only throttling and server/transient errors. Other
                    # 4xx responses (bad token, bad key, invalid request) need
                    # operator action and should fail immediately.
                    try:
                        payload = response.json()
                    except (ValueError, AttributeError, TypeError, requests.RequestException):
                        payload = {}
                    errors = payload.get("errors") if isinstance(payload, dict) else None
                    first_error = errors[0] if errors and isinstance(errors[0], dict) else {}
                    error_code = first_error.get("errorCode") or first_error.get("error_code")
                    detail = first_error.get("message")
                    message = f"Upstox returned HTTP {response.status_code} for {url}"
                    if error_code or detail:
                        message += f" ({error_code or 'provider_error'}: {detail or 'no detail'})"
                    raise UpstoxAPIError(
                        message, status_code=response.status_code, error_code=error_code
                    ) from exc
                return response.json()
            except UpstoxAPIError:
                raise
            except requests.RequestException as exc:
                last_error = exc
                if attempt >= self._request_retries:
                    break
                delay = self._retry_delay(None, attempt)
                limiter.defer(delay)
                logger.warning(
                    "Transient Upstox request error; retry %s/%s after %.1fs: %s",
                    attempt + 1, self._request_retries, delay, exc,
                )
        raise UpstoxAPIError(str(last_error or "Upstox request failed")) from last_error

    # ─── Instrument Master ──────────────────────────────────────────────────

    def download_instruments(self, exchange: str = "NSE") -> pd.DataFrame:
        """
        Download and parse the Upstox instrument master CSV.
        Returns a DataFrame with columns: symbol, instrument_key, isin, company_name, etc.
        """
        url = self.INSTRUMENT_CSV_URL if exchange == "NSE" else self.INSTRUMENT_CSV_BSE_URL
        logger.info(f"Downloading {exchange} instrument master from Upstox...")
        resp = requests.get(url, stream=True, timeout=60)
        resp.raise_for_status()

        df = pd.read_csv(
            io.BytesIO(resp.content),
            compression="gzip",
            dtype=str,
        )
        # Filter equity company stocks only (Upstox uses EQUITY and instrument_key with INE for company stocks)
        is_eq = (df["instrument_type"].isin(["EQ", "EQUITY"])) & (
            df["instrument_key"].str.contains(r"\|INE", na=False)
        )
        df = df[is_eq].copy()
        logger.info(f"Downloaded {len(df)} {exchange} equity instruments.")
        return df

    @classmethod
    def parse_nse_equity_master(cls, csv_content: str | bytes) -> dict[str, dict[str, str]]:
        """Parse NSE's official listed-equity file, keyed by ISIN.

        Only ordinary share-trading series are retained. NSE's published file has
        leading spaces in several CSV headers, so normalize headers and values.
        """
        source = io.BytesIO(csv_content) if isinstance(csv_content, bytes) else io.StringIO(csv_content)
        df = pd.read_csv(source, dtype=str)
        df.columns = [str(column).strip().upper().replace("_", " ") for column in df.columns]
        required = {"SYMBOL", "NAME OF COMPANY", "SERIES", "ISIN NUMBER"}
        if not required.issubset(df.columns):
            raise ValueError(f"NSE equity master missing columns: {sorted(required - set(df.columns))}")

        equities = {}
        for _, row in df.iterrows():
            isin_value = row["ISIN NUMBER"]
            series_value = row["SERIES"]
            if pd.isna(isin_value) or pd.isna(series_value):
                continue
            isin = str(isin_value).strip().upper()
            series = str(series_value).strip().upper()
            if not isin or series not in cls.NSE_SHARE_SERIES:
                continue
            equities[isin] = {
                "symbol": str(row["SYMBOL"]).strip(),
                "company_name": str(row["NAME OF COMPANY"]).strip(),
                "series": series,
            }
        if not equities:
            raise ValueError("NSE equity master contained no recognized share listings")
        return equities

    def download_nse_equity_master(self) -> dict[str, dict[str, str]]:
        """Fetch NSE's main-board and SME share masters, failing closed on bad data."""
        equities = {}
        for url in (self.NSE_EQUITY_MASTER_URL, self.NSE_SME_EQUITY_MASTER_URL):
            response = requests.get(
                url,
                headers={"User-Agent": "Mozilla/5.0", "Accept": "text/csv,*/*"},
                timeout=30,
            )
            response.raise_for_status()
            equities.update(self.parse_nse_equity_master(response.content))
        # A truncated/error page must not deactivate the production universe.
        if len(equities) < self.MIN_NSE_MASTER_EQUITIES:
            raise ValueError(f"NSE equity masters unexpectedly small ({len(equities)} ISINs)")
        logger.info(f"Loaded {len(equities)} main-board/SME shares from NSE equity masters.")
        return equities

    def get_all_eq_symbols(self, exchange: str = "NSE") -> list[dict]:
        """Return NSE ordinary shares cross-checked by ISIN against NSE's equity master."""
        if exchange != "NSE":
            raise ValueError("Only NSE has an authoritative common-equity cross-check configured")
        df = self.download_instruments(exchange)
        nse_equities = self.download_nse_equity_master()
        records = []
        for _, row in df.iterrows():
            inst_key = str(row.get("instrument_key", "")).strip()
            isin = str(row.get("isin", "")).strip()
            if not isin and "|" in inst_key:
                isin = inst_key.split("|", 1)[1]

            equity = nse_equities.get(isin.upper())
            if not equity:
                continue

            records.append({
                "symbol": equity["symbol"],
                "instrument_key": inst_key,
                "isin": isin,
                "company_name": equity["company_name"],
                "exchange": exchange,
                "series": equity["series"],
            })
        if len(records) < self.MIN_MATCHED_NSE_EQUITIES:
            raise ValueError(
                f"Only {len(records)} Upstox instruments matched the NSE share masters; refusing partial refresh"
            )
        logger.info(f"Matched {len(records)} NSE common equities between Upstox and NSE masters.")
        return records

    # ─── Historical OHLCV ───────────────────────────────────────────────────

    def get_historical_candles(
        self,
        instrument_key: str,
        interval: str = "30minute",
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> pd.DataFrame:
        """
        Fetch historical OHLCV candles from Upstox.
        interval: 1minute, 30minute, day, week, month
        Returns DataFrame with columns: ts, open, high, low, close, volume.
        """
        now_ist = datetime.now(IST)
        if to_date is None:
            to_date = now_ist.date().isoformat()
        if from_date is None:
            # Default: last 200 candles worth of data
            days = 200 if interval == "day" else 30
            from_date = (now_ist.date() - timedelta(days=days)).isoformat()

        url = (
            f"{BASE_URL}/historical-candle/{instrument_key}"
            f"/{interval}/{to_date}/{from_date}"
        )
        try:
            data = self._get_json(url, {}, self._historical_limiter)
        except UpstoxAPIError as exc:
            logger.error("Historical candles unavailable for %s: %s", instrument_key, exc)
            raise
        candles = data.get("data", {}).get("candles", [])
        if not candles:
            return pd.DataFrame()

        df = pd.DataFrame(
            candles,
            columns=["ts", "open", "high", "low", "close", "volume", "oi"]
        )
        df["ts"] = pd.to_datetime(df["ts"])
        df = df.sort_values("ts").reset_index(drop=True)
        df[["open", "high", "low", "close", "volume"]] = df[
            ["open", "high", "low", "close", "volume"]
        ].astype(float)
        values = df[["open", "high", "low", "close", "volume"]].to_numpy().ravel()
        if df["ts"].isna().any() or not all(math.isfinite(float(value)) for value in values):
            raise UpstoxAPIError("Upstox returned non-finite candle data")
        invalid = (
            (df[["open", "high", "low", "close"]] <= 0).any(axis=1)
            | (df["volume"] < 0)
            | (df["low"] > df["high"])
            | (df["open"] < df["low"])
            | (df["open"] > df["high"])
            | (df["close"] < df["low"])
            | (df["close"] > df["high"])
        )
        if invalid.any() or df["ts"].duplicated().any():
            raise UpstoxAPIError("Upstox returned inconsistent OHLCV candles")
        return df[["ts", "open", "high", "low", "close", "volume"]]

    def get_daily_candles(self, instrument_key: str, days: int = 365) -> pd.DataFrame:
        """Convenience: fetch daily OHLCV for last N days."""
        from_date = (datetime.now(IST).date() - timedelta(days=days)).isoformat()
        return self.get_historical_candles(instrument_key, "day", from_date)

    def get_option_contracts(self, underlying_key: str) -> list[dict]:
        """Return documented Upstox option contracts for an underlying."""
        data = self._get_json(
            f"{BASE_URL}/option/contract", {"instrument_key": underlying_key}, self._quote_limiter
        )
        contracts = data.get("data") or []
        return contracts if isinstance(contracts, list) else []

    def get_option_chain(self, underlying_key: str, expiry_date: str) -> list[dict]:
        """Return the documented current option-chain snapshot for an expiry."""
        data = self._get_json(
            f"{BASE_URL}/option/chain",
            {"instrument_key": underlying_key, "expiry_date": expiry_date},
            self._quote_limiter,
        )
        rows = data.get("data") or []
        return rows if isinstance(rows, list) else []

    def get_full_market_quotes(
        self, instrument_keys: list[str], batch_size: int = 100, *, allow_missing: bool = False
    ) -> dict[str, dict]:
        """Fetch V3 quote snapshots in provider-supported batches.

        Upstox's V3 ``ohlc`` is a live session snapshot, not a finalized daily
        candle. Its ``close`` can differ from ``last_price``; ``prev_close_price``
        is the prior close. ``ohlc.ts`` is the candle start, so use
        ``last_trade_time`` (epoch milliseconds) for LTP freshness checks.

        Upstox permits up to 500 keys. A default of 100 keeps GET URLs comfortably
        below common proxy limits while still reducing a 3,207-stock sweep to ~33 calls.
        By default the method is all-or-error. A scanner may opt into partial
        results to skip individual instruments without OHLCV while still marking
        the overall sweep incomplete. HTTP/API batch failures always raise.
        """
        if not instrument_keys:
            return {}
        if not 1 <= batch_size <= 500:
            raise ValueError("batch_size must be between 1 and 500")
        result = {}
        expected = set(instrument_keys)
        for offset in range(0, len(instrument_keys), batch_size):
            batch = instrument_keys[offset:offset + batch_size]
            url = f"{BASE_URL_V3}/market-quote/quotes"
            payload = self._get_json(
                url,
                {"instrument_key": ",".join(batch)},
                self._quote_limiter,
            )
            data = payload.get("data", {})
            for quote in data.values():
                key = quote.get("instrument_token")
                ohlc = quote.get("ohlc") or {}
                if key not in expected or not ohlc:
                    continue
                required = ("open", "high", "low", "close", "volume", "ts")
                if any(ohlc.get(field) is None for field in required):
                    continue
                try:
                    mapped = {
                        field: float(ohlc[field])
                        for field in ("open", "high", "low", "close", "volume")
                    }
                    mapped["ts"] = int(ohlc["ts"])
                    # Required separately: never substitute an OHLC field for LTP.
                    if any(quote.get(field) is None for field in (
                        "last_price", "prev_close_price", "last_trade_time"
                    )):
                        continue
                    mapped["last_price"] = float(quote["last_price"])
                    mapped["prev_close_price"] = float(quote["prev_close_price"])
                    mapped["last_trade_time"] = int(quote["last_trade_time"])
                except (TypeError, ValueError, OverflowError):
                    # Treat a malformed instrument record like an omitted symbol;
                    # the scanner reports it and applies the global coverage gate.
                    continue
                result[key] = mapped
        missing = expected - result.keys()
        if missing and not allow_missing:
            raise IncompleteQuoteBatch(set(missing))
        return result

    def get_intraday_candles(
        self, instrument_key: str, interval: str = "30minute"
    ) -> pd.DataFrame:
        """Convenience: fetch intraday candles for today."""
        today = datetime.now(IST).date().isoformat()
        return self.get_historical_candles(instrument_key, interval, today, today)

    # ─── Market Quote (LTP) ─────────────────────────────────────────────────

    def get_ltp(self, instrument_keys: list[str]) -> dict[str, float]:
        """
        Get last traded price for multiple instruments.
        Returns {instrument_key: ltp}.
        Max 500 instruments per call (Upstox limit).
        """
        results = {}
        # Batch into chunks of 500
        for i in range(0, len(instrument_keys), 500):
            batch = instrument_keys[i:i + 500]
            params = {"instrument_key": ",".join(batch)}
            try:
                resp = requests.get(
                    f"{BASE_URL}/market-quote/ltp",
                    headers=self.auth.auth_headers(),
                    params=params,
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json().get("data", {})
                for key, val in data.items():
                    price = float(val.get("last_price", 0))
                    results[key] = price
                    token = val.get("instrument_token")
                    if token:
                        results[token] = price
            except Exception as e:
                logger.error(f"LTP batch error: {e}")
            time.sleep(0.2)  # Respect rate limits
        return results

    # ─── Market Hours Check ─────────────────────────────────────────────────

    @staticmethod
    def is_market_open() -> bool:
        """Check if Indian market is currently open (9:15 AM – 3:30 PM IST, Mon–Fri)."""
        import pytz
        tz = pytz.timezone("Asia/Kolkata")
        now = datetime.now(tz)
        if now.weekday() >= 5:  # Saturday=5, Sunday=6
            return False
        market_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
        market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
        return market_open <= now <= market_close
