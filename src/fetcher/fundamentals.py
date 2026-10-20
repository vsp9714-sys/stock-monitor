"""
Fundamentals fetcher — scrapes Screener.in for company fundamentals.

Screener.in has a JSON API endpoint that provides most fundamental data
without requiring login for basic fields. This is used for:
- Market cap (mcap)
- Cap category (large/mid/small/micro)
- Last 4 quarters net profit
- P/E, P/B, ROE
- Promoter holding

Rate limited to be respectful to the server (configurable delay).
Data is cached in SQLite with a configurable TTL.
"""
import logging
import time
from datetime import datetime, timedelta
from typing import Optional

import requests
from bs4 import BeautifulSoup

from src.config_loader import get_config
from src.db import Fundamental, get_session

logger = logging.getLogger(__name__)

# Cap category thresholds (in Crores) — based on SEBI/AMFI classification
LARGE_CAP_THRESHOLD = 20_000   # > 20,000 Cr = large cap (top ~100 companies)
MID_CAP_THRESHOLD = 5_000      # 5,000–20,000 Cr = mid cap
SMALL_CAP_THRESHOLD = 500      # 500–5,000 Cr = small cap
# Below 500 Cr = micro cap


def classify_cap(mcap_cr: Optional[float]) -> str:
    if mcap_cr is None:
        return "unknown"
    if mcap_cr >= LARGE_CAP_THRESHOLD:
        return "large"
    if mcap_cr >= MID_CAP_THRESHOLD:
        return "mid"
    if mcap_cr >= SMALL_CAP_THRESHOLD:
        return "small"
    return "micro"


class FundamentalsFetcher:
    """Fetches and caches fundamental data from Screener.in."""

    BASE_URL = "https://www.screener.in"

    def __init__(self):
        self.cfg = get_config()
        fund_cfg = self.cfg.get("fundamentals", {})
        self.cache_ttl_hours = fund_cfg.get("cache_ttl_hours", 24)
        self.request_delay = fund_cfg.get("request_delay_seconds", 2.0)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/html, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.screener.in/",
        })

    def get_fundamentals(self, symbol: str, isin: str = "") -> Optional[dict]:
        """
        Get fundamentals for a stock symbol.
        Returns from cache if fresh, else fetches from Screener.in.
        """
        db = get_session()
        try:
            cached = self._get_cached(db, symbol)
            if cached:
                return self._to_dict(cached)
            fetched = self._fetch_from_screener(symbol, isin)
            if fetched:
                self._save_to_db(db, symbol, fetched)
            return fetched
        finally:
            db.close()

    def _get_cached(self, db, symbol: str) -> Optional[Fundamental]:
        """Return cached fundamental if within TTL."""
        fund = db.query(Fundamental).filter_by(symbol=symbol).first()
        if fund and fund.fetched_at:
            age = datetime.utcnow() - fund.fetched_at
            if age < timedelta(hours=self.cache_ttl_hours):
                return fund
        return None

    def _fetch_from_screener(self, symbol: str, isin: str = "") -> Optional[dict]:
        """Fetch fundamental data from Screener.in JSON API."""
        time.sleep(self.request_delay)

        # Screener.in company page URL
        url = f"{self.BASE_URL}/company/{symbol}/consolidated/"
        screener_url = url

        try:
            resp = self.session.get(url, timeout=15)
            if resp.status_code == 404:
                # Try standalone (non-consolidated) version
                url = f"{self.BASE_URL}/company/{symbol}/"
                resp = self.session.get(url, timeout=15)

            if resp.status_code != 200:
                logger.debug(f"Screener.in returned {resp.status_code} for {symbol}")
                return None

            return self._parse_screener_html(resp.text, symbol, screener_url)

        except requests.RequestException as e:
            logger.warning(f"Error fetching Screener.in data for {symbol}: {e}")
            return None

    def _parse_screener_html(self, html: str, symbol: str, screener_url: str) -> Optional[dict]:
        """Parse Screener.in HTML page to extract key fundamentals."""
        try:
            soup = BeautifulSoup(html, "lxml")
            result = {
                "symbol": symbol,
                "screener_url": screener_url,
                "mcap_cr": None,
                "cap_category": "unknown",
                "sector": None,
                "industry": None,
                "pe_ratio": None,
                "pb_ratio": None,
                "roe": None,
                "profit_q1_cr": None,
                "profit_q2_cr": None,
                "profit_q3_cr": None,
                "profit_q4_cr": None,
                "profit_ttm_cr": None,
                "revenue_ttm_cr": None,
                "debt_to_equity": None,
                "promoter_holding_pct": None,
            }

            # ── Market Cap ──────────────────────────────────────────────────
            for li in soup.select("#top-ratios li"):
                label = li.find("span", class_="name")
                value = li.find("span", class_="nowrap")
                if not label or not value:
                    continue
                label_text = label.get_text(strip=True).lower()
                value_text = value.get_text(strip=True).replace(",", "").replace("Cr.", "").strip()

                try:
                    if "market cap" in label_text:
                        result["mcap_cr"] = float(value_text)
                    elif "p/e" in label_text and "industry" not in label_text:
                        result["pe_ratio"] = float(value_text)
                    elif "p/b" in label_text or "price to book" in label_text:
                        result["pb_ratio"] = float(value_text)
                    elif "roe" in label_text or "return on equity" in label_text:
                        result["roe"] = float(value_text.replace("%", ""))
                    elif "debt to equity" in label_text or "d/e" in label_text:
                        result["debt_to_equity"] = float(value_text)
                except (ValueError, TypeError):
                    pass

            # ── Sector / Industry ───────────────────────────────────────────
            company_info = soup.select_one(".company-info")
            if company_info:
                for row in company_info.select(".tag, .sub"):
                    text = row.get_text(strip=True)
                    if result["sector"] is None:
                        result["sector"] = text
                    elif result["industry"] is None:
                        result["industry"] = text
                        break

            # ── Quarterly Profit ────────────────────────────────────────────
            profit_table = None
            for section in soup.select("section"):
                h2 = section.find("h2")
                if h2 and "quarterly" in h2.get_text(strip=True).lower():
                    profit_table = section
                    break

            if profit_table:
                rows = profit_table.select("table tbody tr")
                for row in rows:
                    cells = row.find_all("td")
                    if not cells:
                        continue
                    row_label = cells[0].get_text(strip=True).lower()
                    if "net profit" in row_label or "profit after tax" in row_label:
                        values = []
                        for cell in cells[1:5]:  # Last 4 quarters
                            val_text = cell.get_text(strip=True).replace(",", "")
                            try:
                                values.append(float(val_text))
                            except (ValueError, TypeError):
                                values.append(None)
                        if len(values) >= 1:
                            result["profit_q1_cr"] = values[0] if values else None
                        if len(values) >= 2:
                            result["profit_q2_cr"] = values[1]
                        if len(values) >= 3:
                            result["profit_q3_cr"] = values[2]
                        if len(values) >= 4:
                            result["profit_q4_cr"] = values[3]
                        result["profit_ttm_cr"] = sum(
                            v for v in values[:4] if v is not None
                        ) or None
                        break

            # ── Promoter Holding ─────────────────────────────────────────────
            for row in soup.select("table tbody tr"):
                cells = row.find_all("td")
                if cells and "promoter" in cells[0].get_text(strip=True).lower():
                    try:
                        result["promoter_holding_pct"] = float(
                            cells[-1].get_text(strip=True).replace("%", "")
                        )
                    except (ValueError, TypeError):
                        pass
                    break

            # ── Classify cap category ─────────────────────────────────────────
            result["cap_category"] = classify_cap(result["mcap_cr"])

            return result

        except Exception as e:
            logger.error(f"Error parsing Screener.in HTML for {symbol}: {e}")
            return None

    def _save_to_db(self, db, symbol: str, data: dict) -> None:
        """Upsert fundamental data into the database."""
        existing = db.query(Fundamental).filter_by(symbol=symbol).first()
        if existing:
            obj = existing
        else:
            obj = Fundamental(symbol=symbol)
            db.add(obj)

        for key, val in data.items():
            if hasattr(obj, key):
                setattr(obj, key, val)
        obj.fetched_at = datetime.utcnow()
        db.commit()

    @staticmethod
    def _to_dict(fund: Fundamental) -> dict:
        return {
            "symbol": fund.symbol,
            "mcap_cr": fund.mcap_cr,
            "cap_category": fund.cap_category,
            "sector": fund.sector,
            "industry": fund.industry,
            "pe_ratio": fund.pe_ratio,
            "pb_ratio": fund.pb_ratio,
            "roe": fund.roe,
            "profit_q1_cr": fund.profit_q1_cr,
            "profit_q2_cr": fund.profit_q2_cr,
            "profit_q3_cr": fund.profit_q3_cr,
            "profit_q4_cr": fund.profit_q4_cr,
            "profit_ttm_cr": fund.profit_ttm_cr,
            "revenue_ttm_cr": fund.revenue_ttm_cr,
            "debt_to_equity": fund.debt_to_equity,
            "promoter_holding_pct": fund.promoter_holding_pct,
            "screener_url": fund.screener_url,
        }
