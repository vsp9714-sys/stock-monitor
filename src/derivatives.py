"""Parsing and validating optional, current Upstox option-chain context."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any


def summarize_option_chain(rows: list[dict], *, expiry_date: str, retrieved_at: datetime | None = None) -> dict[str, Any]:
    """Aggregate call/put OI and OI changes, rejecting partial/malformed chains."""
    if not rows:
        return {"status": "unavailable", "reason": "empty_option_chain"}
    try:
        expiry = date.fromisoformat(expiry_date)
        calls = puts = previous_calls = previous_puts = 0.0
        strikes = 0
        for row in rows:
            if row.get("expiry") and date.fromisoformat(str(row["expiry"])) != expiry:
                return {"status": "unavailable", "reason": "mixed_expiries"}
            call = row["call_options"]["market_data"]
            put = row["put_options"]["market_data"]
            values = [float(call["oi"]), float(put["oi"]), float(call["prev_oi"]), float(put["prev_oi"])]
            if any(value < 0 or not value < float("inf") for value in values):
                return {"status": "unavailable", "reason": "invalid_oi_values"}
            calls += values[0]
            puts += values[1]
            previous_calls += values[2]
            previous_puts += values[3]
            strikes += 1
        if not strikes or calls <= 0:
            return {"status": "unavailable", "reason": "missing_call_open_interest"}
        total = calls + puts
        previous_total = previous_calls + previous_puts
        return {
            "status": "available", "source": "Upstox option-chain API",
            "retrieved_at": (retrieved_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(),
            "expiry": expiry.isoformat(), "strike_count": strikes,
            "call_oi": calls, "put_oi": puts, "put_call_oi_ratio": puts / calls,
            "total_oi_change_pct": ((total / previous_total) - 1.0) * 100.0 if previous_total > 0 else None,
        }
    except (KeyError, TypeError, ValueError, OverflowError):
        return {"status": "unavailable", "reason": "malformed_option_chain"}


def nearest_expiry(contracts: list[dict], session_date: date) -> str | None:
    """Choose the nearest listed expiry not earlier than the market session."""
    expiries = set()
    for contract in contracts:
        try:
            expiry = date.fromisoformat(str(contract["expiry"]))
        except (KeyError, TypeError, ValueError):
            continue
        if expiry >= session_date:
            expiries.add(expiry)
    return min(expiries).isoformat() if expiries else None
