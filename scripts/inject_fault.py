#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
INCOMING = ROOT / "data" / "incoming"


def duplicate_pk() -> None:
    path = INCOMING / "orders.csv"
    df = pd.read_csv(path)
    df = pd.concat([df, df.iloc[:3]], ignore_index=True)
    df.to_csv(path, index=False)
    print("Injected duplicate order_id rows.")


def volume_drop() -> None:
    path = INCOMING / "orders.csv"
    df = pd.read_csv(path)
    keep = max(10, int(len(df) * 0.25))
    df.iloc[:keep].to_csv(path, index=False)
    print(f"Injected partial-ingestion fault: kept {keep}/{len(df)} rows.")


def stale_kb() -> None:
    path = INCOMING / "kb_documents.jsonl"
    docs = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for doc in docs:
        ts = pd.to_datetime(doc["published_at"], utc=True)
        doc["published_at"] = (ts - timedelta(hours=3)).isoformat()
    with open(path, "w", encoding="utf-8") as f:
        for row in docs:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print("Injected stale knowledge-base publish timestamps (-3h).")


def scd_break() -> None:
    """Student-added scenario: the SCD close-out job fails.

    The previous version of a customer keeps `is_active = true` instead of being
    closed out. Nothing is null, no primary key is duplicated in orders, every
    contract check passes and the pipeline reports SUCCESS - but the revenue
    mart joins each affected order to two dimension rows and double-counts it.
    This is the "silent wrong number" class of failure the lab is about.
    """
    path = INCOMING / "customers.csv"
    df = pd.read_csv(path)
    active = df[df["is_active"] == True]  # noqa: E712 - csv booleans
    victims = active["customer_id"].head(15).tolist()
    stale_versions = df[df["customer_id"].isin(victims)].copy()
    stale_versions["tier"] = "basic"
    stale_versions["is_active"] = True
    stale_versions["valid_to"] = ""
    df = pd.concat([df, stale_versions], ignore_index=True)
    df.to_csv(path, index=False)
    print(f"Injected SCD close-out failure: {len(victims)} customers now have 2 active rows.")


def policy_rollback() -> None:
    """Student-added scenario: the KB index serves an older policy revision."""
    path = INCOMING / "kb_documents.jsonl"
    docs = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for doc in docs:
        if doc["doc_id"] == "refund-policy":
            doc["version"] = max(1, int(doc["version"]) - 1)
            doc["content"] = "Customers may request a refund within 30 days of purchase."
    with open(path, "w", encoding="utf-8") as f:
        for row in docs:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print("Injected knowledge-base version rollback for refund-policy.")


SCENARIOS = {
    "duplicate_pk": duplicate_pk,
    "volume_drop": volume_drop,
    "stale_kb": stale_kb,
    # Added during the lab - both are "pipeline SUCCESS, numbers wrong" failures.
    "scd_break": scd_break,
    "policy_rollback": policy_rollback,
}

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Inject a public practice fault.")
    parser.add_argument("scenario", choices=sorted(SCENARIOS))
    args = parser.parse_args()
    SCENARIOS[args.scenario]()
