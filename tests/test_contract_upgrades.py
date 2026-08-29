"""Contract checks the starter validator could not make.

Each test names the failure that reached production before the upgrade.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from src.contract_validator import (
    decide_action,
    failed_issues,
    load_contract,
    split_quarantine,
    validate_dataframe,
)

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = load_contract(ROOT / "contracts" / "orders_contract.yaml")


def _now():
    return datetime.now(timezone.utc)


def healthy_df(rows: int = 3) -> pd.DataFrame:
    now = _now()
    return pd.DataFrame(
        [
            {
                "order_id": 100 + i,
                "customer_id": f"C{i:04d}",
                "amount": 10.0 + i,
                "currency": "USD",
                "status": "completed",
                "created_at": (now - timedelta(minutes=20 + i)).strftime("%Y-%m-%dT%H:%M:%S%z"),
                "updated_at": (now - timedelta(minutes=5 + i)).strftime("%Y-%m-%dT%H:%M:%S%z"),
            }
            for i in range(rows)
        ]
    )


def checks(df, **kwargs):
    return {i["check"] for i in failed_issues(validate_dataframe(df, CONTRACT, **kwargs))}


def test_healthy_batch_passes_every_check():
    assert not failed_issues(validate_dataframe(healthy_df(), CONTRACT))


def test_type_drift_is_caught_not_silently_coerced():
    """`amount` arrives as a formatted string. `pd.to_numeric(errors='coerce')`
    turns it into NaN and the starter range check sees nothing wrong."""
    df = healthy_df()
    df["amount"] = df["amount"].astype(object)
    df.loc[0, "amount"] = "1,234.00"
    assert "type" in checks(df)


def test_integer_key_arriving_as_string_is_type_drift():
    df = healthy_df()
    df["order_id"] = df["order_id"].astype(str)
    assert "type" in checks(df)


def test_unparseable_timestamp_is_type_drift():
    df = healthy_df()
    df.loc[1, "created_at"] = "yesterday"
    assert "type" in checks(df)


def test_stale_batch_fails_freshness():
    df = healthy_df()
    old = (_now() - timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%S%z")
    df["updated_at"] = old
    assert "freshness" in checks(df)


def test_fresh_batch_passes_freshness():
    assert "freshness" not in checks(healthy_df())


def test_freshness_reference_time_is_injectable():
    df = healthy_df()
    future = _now() + timedelta(days=1)
    assert "freshness" in checks(df, now=future)


def test_cross_field_rule_catches_updated_before_created():
    df = healthy_df()
    df.loc[0, "updated_at"] = (_now() - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S%z")
    assert "row_rule" in checks(df)


@pytest.mark.parametrize(
    "mutate, expected_action",
    [
        (lambda d: d.assign(order_id=[1, 1, 2]), "block"),          # critical -> block
        (lambda d: d.assign(status=["completed", "shipped", "pending"]), "quarantine"),
    ],
)
def test_severity_drives_the_pipeline_action(mutate, expected_action):
    decision = decide_action(validate_dataframe(mutate(healthy_df()), CONTRACT))
    assert decision["action"] == expected_action


def test_quarantine_isolates_only_the_bad_rows():
    df = healthy_df(5)
    df.loc[2, "status"] = "shipped"          # not in accepted_values
    df.loc[4, "currency"] = "BTC"
    clean, bad = split_quarantine(df, CONTRACT)
    assert len(clean) == 3 and len(bad) == 2
    assert set(bad.index) == {2, 4}
    assert "accepted_values:status" in bad.loc[2, "quarantine_reason"]


def test_missing_required_column_is_reported_once():
    df = healthy_df().drop(columns=["currency"])
    failed = failed_issues(validate_dataframe(df, CONTRACT))
    assert [i["check"] for i in failed].count("required_column") == 1


# ------------------------------------------------------- conditional uniqueness
CUSTOMERS = load_contract(ROOT / "contracts" / "customers_contract.yaml")


def customers_df(extra_active_row: bool = False) -> pd.DataFrame:
    rows = [
        {"customer_id": "C0001", "country": "VN", "tier": "gold",
         "is_active": False, "valid_from": "2024-01-01T00:00:00+0000"},
        {"customer_id": "C0001", "country": "VN", "tier": "gold",
         "is_active": True, "valid_from": "2026-01-01T00:00:00+0000"},
        {"customer_id": "C0002", "country": "SG", "tier": "basic",
         "is_active": True, "valid_from": "2026-01-01T00:00:00+0000"},
    ]
    if extra_active_row:
        rows.append(dict(rows[1], valid_from="2026-06-01T00:00:00+0000"))
    return pd.DataFrame(rows)


def test_repeated_key_across_scd_versions_is_valid():
    """C0001 appears twice - once closed out, once active. That is correct SCD
    history, and a plain column-level `unique` would wrongly reject it."""
    assert not failed_issues(validate_dataframe(customers_df(), CUSTOMERS))


def test_two_active_rows_for_one_customer_is_critical():
    """The failure that inflated revenue 17.1% with no null, no duplicate
    order_id and no SQL error."""
    issues = failed_issues(validate_dataframe(customers_df(True), CUSTOMERS))
    assert [i["check"] for i in issues] == ["unique_together"]
    assert issues[0]["severity"] == "critical"
    assert issues[0]["action"] == "block"
    assert issues[0]["rows_affected"] == 2


def test_broken_scd_rows_are_quarantinable():
    _, bad = split_quarantine(customers_df(True), CUSTOMERS)
    assert len(bad) == 2
    assert "unique_together:one_active_row_per_customer" in bad.iloc[0]["quarantine_reason"]
