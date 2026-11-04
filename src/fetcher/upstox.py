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
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import pandas as pd
import requests

from src.config_loader import get_config

logger = logging.getLogger(__name__)

BASE_URL = "https://api.upstox.com/v2"
TOKEN_CACHE = Path("data/upstox_token.json")


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
        if to_date is None:
            to_date = datetime.now().strftime("%Y-%m-%d")
        if from_date is None:
            # Default: last 200 candles worth of data
            days = 200 if interval == "day" else 30
            from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

        url = (
            f"{BASE_URL}/historical-candle/{instrument_key}"
            f"/{interval}/{to_date}/{from_date}"
        )
        try:
            resp = requests.get(url, headers=self.auth.auth_headers(), timeout=30)
            resp.raise_for_status()
            data = resp.json()
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
            return df[["ts", "open", "high", "low", "close", "volume"]]

        except requests.HTTPError as e:
            logger.warning(f"HTTP error fetching candles for {instrument_key}: {e}")
            return pd.DataFrame()
        except Exception as e:
            logger.error(f"Error fetching candles for {instrument_key}: {e}")
            return pd.DataFrame()

    def get_daily_candles(self, instrument_key: str, days: int = 250) -> pd.DataFrame:
        """Convenience: fetch daily OHLCV for last N days."""
        from_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        return self.get_historical_candles(instrument_key, "day", from_date)

    def get_intraday_candles(
        self, instrument_key: str, interval: str = "30minute"
    ) -> pd.DataFrame:
        """Convenience: fetch intraday candles for today."""
        today = datetime.now().strftime("%Y-%m-%d")
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
