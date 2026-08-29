from datetime import datetime, timedelta, timezone
from pathlib import Path
import pandas as pd

from student_api import validate_orders

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contracts" / "orders_contract.yaml"


def _iso(minutes_ago: int) -> str:
    """Timestamp `minutes_ago` minutes in the past, in UTC ISO-8601."""
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def healthy_df():
    # NOTE(student): the original fixture hard-coded 2026-08-28 timestamps.
    # The contract declares `freshness.max_delay_minutes: 30`, so any
    # freshness-aware validator (required by Phase 1 of the lab) fails on a
    # fixture that is permanently stale. Timestamps are now relative to `now`
    # so the fixture really is "healthy data" on any run date.
    return pd.DataFrame([
        {
            "order_id": 1,
            "customer_id": "C1",
            "amount": 10.0,
            "currency": "USD",
            "status": "completed",
            "created_at": _iso(10),
            "updated_at": _iso(5),
        },
        {
            "order_id": 2,
            "customer_id": "C2",
            "amount": 20.0,
            "currency": "USD",
            "status": "pending",
            "created_at": _iso(9),
            "updated_at": _iso(4),
        },
    ])


def failed(issues):
    return [i for i in issues if not i["passed"]]


def test_healthy_contract_passes_starter_checks():
    assert not failed(validate_orders(healthy_df(), CONTRACT))


def test_duplicate_order_id_is_detected():
    df = healthy_df()
    df.loc[1, "order_id"] = 1
    issues = failed(validate_orders(df, CONTRACT))
    assert any(i["check"] == "unique" and i["column"] == "order_id" for i in issues)


def test_invalid_currency_is_detected():
    df = healthy_df()
    df.loc[0, "currency"] = "BTC"
    issues = failed(validate_orders(df, CONTRACT))
    assert any(i["check"] == "accepted_values" and i["column"] == "currency" for i in issues)
