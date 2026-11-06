"""Independent-group confluence policies for daily and provisional signals."""
from __future__ import annotations

from typing import Any


VALID_CONFLUENCE_MODES = {"crossover", "trend"}
SIGNAL_GROUPS = ("trend", "momentum", "participation")


def signal_group(signal_type: str) -> str:
    """Map correlated checks to their one-vote group."""
    if signal_type.startswith("ema_"):
        return "trend"
    if signal_type in {"rsi", "macd"}:
        return "momentum"
    if signal_type == "volume_breakout":
        return "participation"
    return "other"


def evaluate_confluence(
    checks: list[dict[str, Any]],
    *,
    mode: str = "crossover",
    minimum_groups: int = 2,
    direction_consensus: bool = True,
) -> dict[str, Any]:
    """Apply one policy, then count no more than one vote per independent group.

    Checks carry separate event and state outcomes. ``crossover`` selects only
    fresh events; ``trend`` selects current trend states. Same-group opposing
    directions suppress that group's vote and remain visible in diagnostics.
    """
    if mode not in VALID_CONFLUENCE_MODES:
        raise ValueError(f"Unsupported confluence mode: {mode}")
    if not isinstance(minimum_groups, int) or not 1 <= minimum_groups <= len(SIGNAL_GROUPS):
        raise ValueError("confluence.min_groups_to_alert must be an integer from 1 to 3")

    evaluated: list[dict[str, Any]] = []
    active_groups: set[str] = set()
    for original in checks:
        check = dict(original)
        prior_met = bool(check.get("met", False))
        group = signal_group(str(check.get("signal_type", "")))
        check["group"] = group
        unavailable = check.get("availability") == "unavailable"
        if group != "other" and not unavailable:
            active_groups.add(group)
        if mode == "crossover":
            check["met"] = bool(check.get("event_met", check.get("met", False)))
            check["direction"] = check.get("event_direction", check.get("direction"))
        else:
            check["met"] = bool(check.get("state_met", check.get("met", False)))
            check["direction"] = check.get("state_direction", check.get("direction"))
        if unavailable:
            check["met"] = False
            check["direction"] = None
        check["group_vote"] = False
        if mode == "trend" and check["met"]:
            check["description"] = (
                f"Trend continuation {check['direction']}: {check.get('description', '')}"
            ).strip()
        elif mode == "trend" and not check["met"] and check.get("event_met"):
            check["description"] = (
                f"Fresh event only; no continuation state: {check.get('description', '')}"
            ).strip()
        elif mode == "crossover" and not check["met"] and check.get("state_met"):
            check["description"] = (
                f"Aligned state only; no fresh event: {check.get('description', '')}"
            ).strip()
        elif mode == "crossover" and not check["met"] and prior_met:
            check["description"] = (
                f"Condition persists; no fresh event: {check.get('description', '')}"
            ).strip()
        evaluated.append(check)

    directions_by_group: dict[str, set[str]] = {}
    for check in evaluated:
        if check["met"] and check.get("direction") in {"BUY", "SELL"}:
            directions_by_group.setdefault(check["group"], set()).add(check["direction"])

    votes: dict[str, str] = {}
    conflicting_groups: list[str] = []
    for group, directions in directions_by_group.items():
        if len(directions) == 1:
            votes[group] = next(iter(directions))
            # Keep every raw match for audit, but mark only one deterministic
            # representative as the group's actual vote.
            representative = next(
                check for check in evaluated
                if check["group"] == group and check["met"]
                and check.get("direction") == next(iter(directions))
            )
            representative["group_vote"] = True
            representative["description"] = (
                f"{representative.get('description', '')} (counted as {group} group vote)"
            ).strip()
            for check in evaluated:
                if check["group"] == group and check["met"] and check is not representative:
                    check["description"] = (
                        f"{check.get('description', '')} (additional check; group counted once)"
                    ).strip()
        else:
            conflicting_groups.append(group)

    buy_groups = [group for group, direction in votes.items() if direction == "BUY"]
    sell_groups = [group for group, direction in votes.items() if direction == "SELL"]
    direction = None
    score = 0
    if not (direction_consensus and buy_groups and sell_groups):
        if len(buy_groups) > len(sell_groups):
            direction, score = "BUY", len(buy_groups)
        elif len(sell_groups) > len(buy_groups):
            direction, score = "SELL", len(sell_groups)
    if score < minimum_groups:
        direction = None

    return {
        "direction": direction,
        "score": score,
        "total_groups": len(active_groups),
        "group_votes": votes,
        "conflicting_groups": sorted(conflicting_groups),
        "checks": evaluated,
    }
