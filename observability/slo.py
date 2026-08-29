"""SLI / SLO / error-budget maths and a multi-window burn-rate alerting policy.

Reference: Google SRE Workbook, "Alerting on SLOs" - multiwindow,
multi-burn-rate alerts. The point of the long window is *significance* (is the
budget really being consumed?) and the point of the short window is *recency*
(is it still burning right now?). Requiring both prevents two opposite
failures: paging on a 5-minute blip, and keeping someone paged for an incident
that already recovered.
"""
from __future__ import annotations

from typing import Any, Iterable

#: (long-window burn threshold, budget consumed in the window, alert class).
#: Derived from the workbook's 2%/1h, 5%/6h, 10%/3d table for a 30-day SLO.
BURN_POLICY: tuple[dict[str, Any], ...] = (
    {"threshold": 14.4, "alert_class": "page", "severity": "critical",
     "budget_burned": "2% of a 30-day budget in 1 hour", "long_window": "1h", "short_window": "5m"},
    {"threshold": 6.0, "alert_class": "page", "severity": "high",
     "budget_burned": "5% of a 30-day budget in 6 hours", "long_window": "6h", "short_window": "30m"},
    {"threshold": 3.0, "alert_class": "ticket", "severity": "warning",
     "budget_burned": "10% of a 30-day budget in 1 day", "long_window": "1d", "short_window": "2h"},
    {"threshold": 1.0, "alert_class": "ticket", "severity": "info",
     "budget_burned": "10% of a 30-day budget in 3 days", "long_window": "3d", "short_window": "6h"},
)


def calculate_slo(target: float, bad_events: int, total_events: int) -> dict[str, Any]:
    """Error-budget status for one measurement window."""
    if not 0 < target < 1:
        raise ValueError("target must be between 0 and 1 (exclusive)")
    if bad_events < 0 or total_events < 0 or bad_events > total_events:
        raise ValueError("invalid event counts")

    allowed_bad_rate = 1.0 - target
    if total_events == 0:
        return {
            "target": target,
            "actual_bad_rate": 0.0,
            "allowed_bad_rate": allowed_bad_rate,
            "burn_rate": 0.0,
            "remaining_error_budget_fraction": 1.0,
            "breached": False,
            "good_events": 0,
            "bad_events": 0,
            "total_events": 0,
            "error_budget_events": 0.0,
            "remaining_error_budget_events": 0.0,
            "sli": 1.0,
        }

    actual_bad_rate = bad_events / total_events
    burn_rate = actual_bad_rate / allowed_bad_rate
    consumed_fraction = min(1.0, burn_rate)
    error_budget_events = allowed_bad_rate * total_events
    return {
        "target": target,
        "actual_bad_rate": actual_bad_rate,
        "allowed_bad_rate": allowed_bad_rate,
        "burn_rate": burn_rate,
        "remaining_error_budget_fraction": max(0.0, 1.0 - consumed_fraction),
        "breached": bool(actual_bad_rate > allowed_bad_rate),
        "good_events": int(total_events - bad_events),
        "bad_events": int(bad_events),
        "total_events": int(total_events),
        "error_budget_events": float(error_budget_events),
        "remaining_error_budget_events": float(max(0.0, error_budget_events - bad_events)),
        "sli": 1.0 - actual_bad_rate,
    }


def burn_rate(target: float, bad_events: int, total_events: int) -> float:
    return calculate_slo(target, bad_events, total_events)["burn_rate"]


def evaluate_multiwindow_burn(
    *,
    short_window_burn: float,
    long_window_burn: float,
    policy: str = "google_sre",
    short_window_factor: float = 1.0,
    long_window_events: int | None = None,
    min_long_window_events: int = 5,
) -> dict[str, Any]:
    """Decide whether a burn-rate pattern deserves a page, a ticket, or nothing.

    The long window must clear a tier threshold (the budget really is being
    consumed) **and** the short window must still be burning at
    ``short_window_factor x threshold`` (it is still happening now).

    * sustained fast burn  -> page,
    * short transient spike (long window quiet) -> no page,
    * long window hot but short window recovered -> no page, ticket to follow up.
    """
    if policy != "google_sre":
        raise ValueError(f"Unsupported policy: {policy}")

    short = float(short_window_burn)
    long = float(long_window_burn)
    base = {
        "short_window_burn": short,
        "long_window_burn": long,
        "policy": policy,
    }

    # Significance guard: a single bad check in a two-check window produces a
    # 1000x burn rate. That is arithmetically true and operationally useless.
    # When the caller knows how many events the long window contains, refuse to
    # page until the window is statistically meaningful.
    if long_window_events is not None and long_window_events < min_long_window_events:
        return {
            **base,
            "page": False,
            "ticket": False,
            "alert_class": "none",
            "severity": "info",
            "threshold": BURN_POLICY[-1]["threshold"],
            "burning_now": short > 1.0,
            "reason": (
                f"insufficient data: long window has {long_window_events} event(s), "
                f"needs >= {min_long_window_events} before a burn rate is significant"
            ),
            "recommended_action": "collect more checks before alerting",
            "long_window_events": long_window_events,
        }

    for tier in BURN_POLICY:
        threshold = tier["threshold"]
        if long < threshold:
            continue
        short_ok = short >= threshold * short_window_factor
        if short_ok:
            page = tier["alert_class"] == "page"
            return {
                **base,
                "page": page,
                "ticket": not page,
                "alert_class": tier["alert_class"],
                "severity": tier["severity"],
                "threshold": threshold,
                "burning_now": True,
                "reason": (
                    f"sustained burn: long_window={long:.2f}x and short_window={short:.2f}x "
                    f"both >= {threshold}x ({tier['budget_burned']}); "
                    f"windows={tier['long_window']}/{tier['short_window']}"
                ),
                "recommended_action": (
                    "page on-call, stop the bleeding, then verify recovery"
                    if page
                    else "open a ticket, fix within the next working day"
                ),
            }
        return {
            **base,
            "page": False,
            "ticket": True,
            "alert_class": "ticket",
            "severity": "warning",
            "threshold": threshold,
            "burning_now": False,
            "reason": (
                f"budget already consumed (long_window={long:.2f}x >= {threshold}x) but the "
                f"short window recovered (short_window={short:.2f}x). No page: nothing is "
                f"burning right now. Follow up on the budget that was spent."
            ),
            "recommended_action": "ticket: review consumed error budget and root cause",
        }

    if short >= BURN_POLICY[0]["threshold"]:
        return {
            **base,
            "page": False,
            "ticket": False,
            "alert_class": "none",
            "severity": "info",
            "threshold": BURN_POLICY[0]["threshold"],
            "burning_now": True,
            "reason": (
                f"transient spike: short_window={short:.2f}x is high but long_window={long:.2f}x "
                f"is below every tier threshold, so total budget impact is not significant yet"
            ),
            "recommended_action": "no page; keep observing, alert if it persists",
        }

    return {
        **base,
        "page": False,
        "ticket": False,
        "alert_class": "none",
        "severity": "info",
        "threshold": BURN_POLICY[-1]["threshold"],
        "burning_now": False,
        "reason": f"healthy: short_window={short:.2f}x, long_window={long:.2f}x below all thresholds",
        "recommended_action": "none",
    }


def rolling_burn(
    target: float, outcomes: Iterable[bool], *, short_window: int, long_window: int
) -> dict[str, Any]:
    """Compute short/long burn rates from a stream of per-check outcomes.

    ``outcomes`` is oldest-first; ``True`` marks a *bad* check.
    """
    events = [bool(o) for o in outcomes]
    short_slice = events[-short_window:] if short_window else []
    long_slice = events[-long_window:] if long_window else []
    short = calculate_slo(target, sum(short_slice), len(short_slice))
    long = calculate_slo(target, sum(long_slice), len(long_slice))
    decision = evaluate_multiwindow_burn(
        short_window_burn=short["burn_rate"], long_window_burn=long["burn_rate"]
    )
    return {
        "short_window": short,
        "long_window": long,
        "decision": decision,
        "short_window_size": len(short_slice),
        "long_window_size": len(long_slice),
    }
