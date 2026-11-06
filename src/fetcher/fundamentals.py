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
import re
import time
from datetime import datetime, timedelta, timezone
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


def _parse_indian_number(text: str) -> Optional[float]:
    """
    Parse Indian-format numbers from Screener.in.
    Handles: '17,71,283', '1,308', '21.6', '0.46%', '₹17,71,283Cr.', '₹ 3,450.50 Cr', empty strings.
    """
    if not text:
        return None
    cleaned = text.strip()
    # Remove currency symbol, Cr/cr, %, whitespace
    cleaned = re.sub(r'₹', '', cleaned)
    cleaned = re.sub(r'Cr\.?', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'%', '', cleaned)
    cleaned = cleaned.strip()
    if not cleaned or cleaned == '-':
        return None
    # Remove commas
    cleaned = cleaned.replace(',', '')
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return None


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
                return self._to_dict(cached, status="fresh")
            stale = db.query(Fundamental).filter_by(symbol=symbol).first()
            fetched = self._fetch_from_screener(symbol, isin)
            if fetched:
                self._save_to_db(db, symbol, fetched)
                saved = db.query(Fundamental).filter_by(symbol=symbol).first()
                if saved:
                    result = self._to_dict(saved, status="fresh")
                    result["quarterly_data_status"] = fetched.get("quarterly_data_status", "unknown")
                    return result
                return fetched
            # A failed refresh must not erase usable history. Return the old
            # record with explicit stale metadata rather than presenting it as
            # current or silently dropping all fundamentals from the alert.
            return self._to_dict(stale, status="stale") if stale else None
        finally:
            db.close()

    def _get_cached(self, db, symbol: str) -> Optional[Fundamental]:
        """Return cached fundamental if within TTL."""
        fund = db.query(Fundamental).filter_by(symbol=symbol).first()
        if fund and fund.fetched_at:
            age = datetime.now(timezone.utc) - fund.fetched_at.replace(tzinfo=timezone.utc)
            if age < timedelta(hours=self.cache_ttl_hours):
                return fund
        return None

    def _fetch_from_screener(self, symbol: str, isin: str = "") -> Optional[dict]:
        """Fetch fundamental data from Screener.in HTML page."""
        time.sleep(self.request_delay)

        # Screener.in company page URL
        url = f"{self.BASE_URL}/company/{symbol}/consolidated/"
        screener_url = url

        try:
            resp = self.session.get(url, timeout=15)
            if resp.status_code == 404:
                # Try standalone (non-consolidated) version
                url = f"{self.BASE_URL}/company/{symbol}/"
                screener_url = url
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
                "quarterly_data_status": "unavailable",
            }

            # ── Top Ratios (Market Cap, P/E, ROE, etc.) ──────────────────────
            current_price = None
            book_value = None

            for li in soup.select("#top-ratios li"):
                label_el = li.find("span", class_="name")
                if not label_el:
                    continue
                label_text = label_el.get_text(strip=True).lower()

                # Prefer <span class="number"> (clean value) over <span class="nowrap">
                number_el = li.find("span", class_="number")
                nowrap_el = li.find("span", class_="nowrap")

                # Get raw text from the cleanest source available
                if number_el:
                    raw_val = number_el.get_text(strip=True)
                elif nowrap_el:
                    raw_val = nowrap_el.get_text(strip=True)
                else:
                    continue

                parsed = _parse_indian_number(raw_val)
                if parsed is None:
                    continue

                try:
                    if "market cap" in label_text:
                        result["mcap_cr"] = parsed
                    elif "current price" in label_text:
                        current_price = parsed
                    elif label_text.startswith("stock p/e") or label_text == "p/e":
                        result["pe_ratio"] = parsed
                    elif "book value" in label_text:
                        book_value = parsed
                    elif "price to book" in label_text or label_text == "p/b":
                        result["pb_ratio"] = parsed
                    elif "roe" in label_text or "return on equity" in label_text:
                        result["roe"] = parsed
                    elif "debt to equity" in label_text or label_text == "d/e":
                        result["debt_to_equity"] = parsed
                except (ValueError, TypeError):
                    pass

            if result["pb_ratio"] is None and current_price and book_value and book_value > 0:
                result["pb_ratio"] = round(current_price / book_value, 2)

            # ── Sector / Industry ───────────────────────────────────────────
            # Screener.in peers section has hierarchy: <section id="peers"><p class="sub"><a href="/market/...">Sector</a>...
            peers_section = soup.find("section", id="peers")
            if peers_section:
                sub_p = peers_section.find("p", class_="sub")
                if sub_p:
                    market_links = sub_p.find_all("a", href=lambda h: h and "/market/" in h)
                    if market_links:
                        result["sector"] = market_links[0].get_text(strip=True)
                        result["industry"] = market_links[-1].get_text(strip=True)

            # Fallback for sector/industry if peers section didn't match
            if not result["sector"]:
                company_info = soup.select_one(".company-info")
                if company_info:
                    for link in company_info.select("a"):
                        href = link.get("href", "")
                        text = link.get_text(strip=True)
                        if not text or len(text) > 80:
                            continue
                        if "/sector/" in href or "/industry/" in href or "/market/" in href:
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
                table = profit_table.select_one("table")
                headers = [cell.get_text(" ", strip=True) for cell in table.select("thead th")][1:] if table else []
                periods = []
                for label in headers:
                    parsed_period = None
                    for fmt in ("%b %Y", "%B %Y"):
                        try:
                            parsed_period = datetime.strptime(label, fmt)
                            break
                        except ValueError:
                            pass
                    if parsed_period is None:
                        periods = []
                        break
                    periods.append(parsed_period)
                periods_valid = bool(periods) and periods == sorted(set(periods))
                rows = profit_table.select("table tbody tr")
                for row in rows:
                    cells = row.find_all("td")
                    if not cells:
                        continue
                    row_label = cells[0].get_text(strip=True).lower()
                    if "net profit" in row_label or "profit after tax" in row_label:
                        if not periods_valid or len(cells) - 1 != len(periods):
                            result["quarterly_data_status"] = "invalid_period_order"
                            break
                        # Get last 4 quarters (rightmost columns = most recent)
                        values = []
                        for cell in cells[-4:]:
                            val = _parse_indian_number(cell.get_text(strip=True))
                            values.append(val)
                        if len(values) >= 4:
                            result["profit_q4_cr"] = values[0]
                            result["profit_q3_cr"] = values[1]
                            result["profit_q2_cr"] = values[2]
                            result["profit_q1_cr"] = values[3]
                        result["profit_ttm_cr"] = sum(
                            v for v in values if v is not None
                        ) or None
                        result["quarterly_data_status"] = "available" if any(v is not None for v in values) else "unavailable"
                        break

            # ── Promoter Holding ─────────────────────────────────────────────
            # Look specifically in the shareholding section
            for section in soup.select("section"):
                h2 = section.find("h2")
                if h2 and "shareholding" in h2.get_text(strip=True).lower():
                    for row in section.select("table tbody tr"):
                        cells = row.find_all("td")
                        if cells and "promoter" in cells[0].get_text(strip=True).lower():
                            # Get the most recent quarter's value (last column)
                            val = _parse_indian_number(cells[-1].get_text(strip=True))
                            if val is not None:
                                result["promoter_holding_pct"] = val
                            break
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
        obj.fetched_at = datetime.now(timezone.utc)
        db.commit()

    @staticmethod
    def _to_dict(fund: Fundamental, *, status: str = "unknown") -> dict:
        fetched_at = fund.fetched_at
        if fetched_at and fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        age_seconds = max(0.0, (datetime.now(timezone.utc) - fetched_at).total_seconds()) if fetched_at else None
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
            "source_url": fund.screener_url,
            "fetched_at": fetched_at.isoformat() if fetched_at else None,
            "cache_age_seconds": age_seconds,
            "cache_age_hours": age_seconds / 3600.0 if age_seconds is not None else None,
            "freshness": status if fetched_at else "unknown",
            "quarterly_data_status": "unknown",
            "data_basis": (
                "consolidated" if fund.screener_url and "/consolidated/" in fund.screener_url
                else "standalone" if fund.screener_url else "unknown"
            ),
        }
