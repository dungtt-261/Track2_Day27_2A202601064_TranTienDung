#!/usr/bin/env python3
from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "data" / "baseline"
INCOMING = ROOT / "data" / "incoming"


def shift_dataframe_timestamps(df: pd.DataFrame, columns: list[str], target_age_minutes: int = 5) -> pd.DataFrame:
    parsed = []
    for col in columns:
        if col in df.columns:
            parsed.append(pd.to_datetime(df[col], utc=True, errors="coerce"))
    if not parsed:
        return df
    latest = max(s.max() for s in parsed if s.notna().any())
    target = pd.Timestamp(datetime.now(timezone.utc) - timedelta(minutes=target_age_minutes))
    delta = target - latest
    for col in columns:
        if col in df.columns:
            s = pd.to_datetime(df[col], utc=True, errors="coerce")
            df[col] = (s + delta).dt.strftime("%Y-%m-%dT%H:%M:%S%z")
    return df


def seasonal_target_rows(history: pd.DataFrame, weekday: int, available: int) -> int:
    """Row count a *healthy* batch should have for this weekday.

    The shipped baseline always contains the same number of orders, but
    `metrics_history.csv` says weekends run at ~43% of weekday volume. Copying
    the file verbatim therefore produces a "healthy" batch that contradicts its
    own history: on a Saturday the seasonality-aware detector correctly reports a
    2.4x volume spike and the whole lab starts from a false INCIDENT.
    Scaling the healthy batch to the same-weekday median removes the artifact
    without weakening any detector.
    """
    same_weekday = history.loc[history["day_of_week"] == weekday, "row_count"]
    if len(same_weekday) < 3:
        return available
    return int(min(available, max(10, round(float(same_weekday.median())))))


def main() -> None:
    INCOMING.mkdir(parents=True, exist_ok=True)
    orders = pd.read_csv(BASE / "orders.csv")
    history = pd.read_csv(ROOT / "data" / "history" / "metrics_history.csv")
    target = seasonal_target_rows(history, datetime.now(timezone.utc).weekday(), len(orders))
    if target < len(orders):
        orders = orders.sample(n=target, random_state=27).sort_values("order_id").reset_index(drop=True)
    orders = shift_dataframe_timestamps(orders, ["created_at", "updated_at"], target_age_minutes=5)
    orders.to_csv(INCOMING / "orders.csv", index=False)

    shutil.copy2(BASE / "customers.csv", INCOMING / "customers.csv")

    docs = []
    with open(BASE / "kb_documents.jsonl", "r", encoding="utf-8") as f:
        for line in f:
            docs.append(json.loads(line))
    # Re-anchor publish times so the starter dataset is always fresh when class runs.
    now = datetime.now(timezone.utc)
    for i, doc in enumerate(docs):
        doc["published_at"] = (now - timedelta(minutes=10 + i * 2)).isoformat()
    with open(INCOMING / "kb_documents.jsonl", "w", encoding="utf-8") as f:
        for row in docs:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    # Keep dbt seeds synchronized with current incoming data.
    seeds = ROOT / "dbt_project" / "seeds"
    seeds.mkdir(parents=True, exist_ok=True)
    shutil.copy2(INCOMING / "orders.csv", seeds / "orders.csv")
    shutil.copy2(INCOMING / "customers.csv", seeds / "customers.csv")

    metrics = ROOT / "reports" / "latest_metrics.json"
    if metrics.exists():
        metrics.unlink()
    print(f"Lab reset to a healthy baseline ({len(orders)} orders for weekday "
          f"{datetime.now(timezone.utc).weekday()}).")


if __name__ == "__main__":
    main()
