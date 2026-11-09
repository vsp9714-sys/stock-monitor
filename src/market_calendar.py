"""NSE session-time helpers backed by a versioned exchange calendar."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import yaml

IST = ZoneInfo("Asia/Kolkata")
CALENDAR_FILE = Path(__file__).resolve().parents[1] / "config" / "nse_trading_calendar.yaml"


class CalendarUnavailable(ValueError):
    """No validated exchange calendar is available for the requested year."""


@lru_cache(maxsize=1)
def _load_calendar() -> dict:
    with CALENDAR_FILE.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def session_window(day: date, market_cfg: Optional[dict] = None) -> Optional[tuple[time, time]]:
    """Return the configured NSE session window, or None when the exchange is closed.

    Unknown years fail closed. A special session is active only when explicit
    open/close times have been added to the versioned calendar.
    """
    calendar = _load_calendar()
    years = calendar.get("years", {})
    year = years.get(day.year) or years.get(str(day.year))
    if year is None:
        raise CalendarUnavailable(f"NSE calendar for {day.year} is not configured")

    cfg = market_cfg or {}
    specials = year.get("special_sessions", {}) or {}
    special = specials.get(day.isoformat())
    if special is not None:
        if not special.get("open_time") or not special.get("close_time"):
            return None
        return time.fromisoformat(special["open_time"]), time.fromisoformat(special["close_time"])

    allowed_days = set(cfg.get("trading_days", [0, 1, 2, 3, 4]))
    if day.weekday() not in allowed_days:
        return None

    holidays = year.get("holidays", [])
    holiday_dates = {entry["date"] if isinstance(entry, dict) else str(entry) for entry in holidays}
    if day.isoformat() in holiday_dates:
        return None

    return (
        time.fromisoformat(cfg.get("open_time", "09:15")),
        time.fromisoformat(cfg.get("close_time", "15:30")),
    )


def is_trading_day(day: date, market_cfg: Optional[dict] = None) -> bool:
    return session_window(day, market_cfg) is not None


def is_valid_candle_day(day: date, market_cfg: Optional[dict] = None) -> bool:
    """Validate a historical daily bar date; unknown historical years at least reject weekends.

    Refresh target selection still uses ``is_trading_day`` and therefore fails
    closed for an unconfigured year. This fallback is only for filtering
    historical cache rows outside the configured calendar coverage.
    """
    try:
        return is_trading_day(day, market_cfg)
    except CalendarUnavailable:
        return day.weekday() < 5


def latest_completed_session_day(now: datetime, market_cfg: Optional[dict] = None) -> date:
    """Return the latest calendar-valid NSE session that should be complete.

    Before the configured close plus a 15-minute finalization buffer, the
    current session is not considered complete. This also makes a weekend or
    holiday refresh resolve to the preceding actual session.
    """
    current = now
    if current.tzinfo is None:
        current = current.replace(tzinfo=ZoneInfo("UTC"))
    current = current.astimezone(IST)
    current_day = current.date()
    window = session_window(current_day, market_cfg)
    if window is not None:
        _, close_time = window
        finalization_cutoff = (
            datetime.combine(current_day, close_time, tzinfo=IST) + timedelta(minutes=15)
        )
        if current < finalization_cutoff:
            current_day -= timedelta(days=1)

    for _ in range(370):
        if is_trading_day(current_day, market_cfg):
            return current_day
        current_day -= timedelta(days=1)
    raise CalendarUnavailable("No completed NSE session found in the preceding 370 days")


def is_market_open(now: Optional[datetime] = None, market_cfg: Optional[dict] = None) -> bool:
    """Check the exchange session in IST; naive supplied datetimes are treated as UTC."""
    current = now or datetime.now(IST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=ZoneInfo("UTC"))
    current = current.astimezone(IST)
    window = session_window(current.date(), market_cfg)
    if window is None:
        return False
    open_time, close_time = window
    return open_time <= current.time().replace(tzinfo=None) <= close_time
