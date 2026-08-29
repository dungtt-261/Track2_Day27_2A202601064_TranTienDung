#!/usr/bin/env python3
"""One reliability run: validate -> detect -> locate -> quantify -> decide.

Every signal the incident commander needs is produced here and persisted to
``reports/latest_metrics.json`` (dashboard + incident report) and appended to
``reports/run_history.jsonl`` (so short/long burn-rate windows have real data).

Layers, in the order they are able to catch a failure:

1. **Contract**  - deterministic rules (nulls, PK, domain, type, freshness).
2. **Anomaly**   - statistical, for failures nobody wrote a rule for.
3. **Lineage**   - who is already serving wrong numbers.
4. **SLO**       - is this worth waking somebody up.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from observability.anomaly import detect_anomaly  # noqa: E402
from observability.distribution import detect_distribution_shift  # noqa: E402
from observability.lineage import blast_radius, load_column_graph, load_graph  # noqa: E402
from observability.rag_metrics import (  # noqa: E402
    detect_text_length_shift,
    detect_version_regression,
    kb_staleness,
)
from observability.slo import calculate_slo, evaluate_multiwindow_burn  # noqa: E402
from src.contract_validator import (  # noqa: E402
    decide_action,
    failed_issues,
    load_contract,
    split_quarantine,
    validate_dataframe,
    validate_records,
)
from src.io_utils import load_jsonl  # noqa: E402

SLO_CONTRACT_TARGET = 0.999
SLO_FRESHNESS_TARGET = 0.995
SLO_KB_TARGET = 0.99
FRESHNESS_THRESHOLD_MINUTES = 30
KB_FRESHNESS_THRESHOLD_MINUTES = 60
SHORT_WINDOW_RUNS = 3
LONG_WINDOW_RUNS = 20


def _history_context(history: pd.DataFrame, now: datetime) -> dict[str, Any]:
    """Same-weekday baseline, so a quiet Saturday is not treated as an outage."""
    dow = now.weekday()
    segment = history.loc[history["day_of_week"] == dow, "row_count"].tail(8).tolist()
    return {
        "metric_name": "row_count",
        "day_of_week": dow,
        "same_segment_history": segment,
        "history_day_of_week": history["day_of_week"].tolist(),
    }


def _append_run_history(path: Path, record: dict[str, Any]) -> list[dict[str, Any]]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")
    runs = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                runs.append(json.loads(line))
    return runs


def _window_slo(runs: list[dict[str, Any]], key: str, target: float, size: int) -> dict[str, Any]:
    window = runs[-size:]
    bad = sum(1 for r in window if r.get(key))
    return calculate_slo(target, bad_events=bad, total_events=len(window))


def main() -> int:
    now = datetime.now(timezone.utc)
    orders = pd.read_csv(ROOT / "data" / "incoming" / "orders.csv")
    baseline_orders = pd.read_csv(ROOT / "data" / "baseline" / "orders.csv")
    history = pd.read_csv(ROOT / "data" / "history" / "metrics_history.csv")

    # ---------------------------------------------------------------- layer 1
    orders_contract = load_contract(ROOT / "contracts" / "orders_contract.yaml")
    issues = validate_dataframe(orders, orders_contract, now=now)
    failed = failed_issues(issues)
    critical_failed = failed_issues(issues, min_severity="critical")
    decision = decide_action(issues)

    quarantined = 0
    if decision["action"] in {"block", "quarantine"}:
        clean, bad = split_quarantine(orders, orders_contract)
        quarantined = len(bad)
        if quarantined:
            out_dir = ROOT / "data" / "quarantine"
            out_dir.mkdir(parents=True, exist_ok=True)
            bad.to_csv(out_dir / f"orders_quarantine_{now:%Y%m%dT%H%M%SZ}.csv", index=False)

    customers = pd.read_csv(ROOT / "data" / "incoming" / "customers.csv")
    customers_contract = load_contract(ROOT / "contracts" / "customers_contract.yaml")
    customer_issues = validate_dataframe(customers, customers_contract, now=now)
    customer_failed = failed_issues(customer_issues)
    customer_decision = decide_action(customer_issues)

    kb_docs = load_jsonl(ROOT / "data" / "incoming" / "kb_documents.jsonl")
    baseline_docs = load_jsonl(ROOT / "data" / "baseline" / "kb_documents.jsonl")
    kb_contract = load_contract(ROOT / "contracts" / "kb_contract.yaml")
    kb_issues = validate_records(kb_docs, kb_contract, now=now)
    kb_failed = failed_issues(kb_issues)
    kb_decision = decide_action(kb_issues)

    # ---------------------------------------------------------------- layer 2
    row_result = detect_anomaly(
        len(orders),
        history["row_count"].tail(28).tolist(),
        method="auto",
        context=_history_context(history, now),
    )
    amount_result = detect_distribution_shift(
        orders["amount"].tolist(), baseline_orders["amount"].tolist()
    )
    currency_result = detect_distribution_shift(
        orders["currency"].astype(str).tolist(), baseline_orders["currency"].astype(str).tolist()
    )
    status_result = detect_distribution_shift(
        orders["status"].astype(str).tolist(), baseline_orders["status"].astype(str).tolist()
    )

    updated = pd.to_datetime(orders["updated_at"], utc=True, errors="coerce", format="mixed")
    freshness_minutes = (pd.Timestamp(now) - updated.max()).total_seconds() / 60.0

    kb_fresh = kb_staleness(
        kb_docs, max_delay_minutes=KB_FRESHNESS_THRESHOLD_MINUTES, now=now
    )
    kb_version = detect_version_regression(kb_docs, baseline_docs)
    text_result = detect_text_length_shift(
        [d["content"] for d in kb_docs], history["mean_text_length"].tail(14).tolist()
    )

    # ---------------------------------------------------------------- layer 3
    lineage = load_graph(ROOT / "data" / "baseline" / "lineage_graph.json")
    column_lineage = load_column_graph(ROOT / "data" / "baseline" / "lineage_graph.json")
    orders_blast = blast_radius(
        lineage, "stg_orders", column_graph=column_lineage,
        start_columns=["stg_orders.amount_usd"],
    )
    kb_blast = blast_radius(lineage, "kb_documents", column_graph=column_lineage)

    # ---------------------------------------------------------------- layer 4
    orders_bad = bool(critical_failed) or bool(row_result["is_anomaly"])
    freshness_bad = freshness_minutes > FRESHNESS_THRESHOLD_MINUTES
    kb_bad = bool(kb_fresh["is_anomaly"]) or bool(failed_issues(kb_issues, "critical")) or bool(
        kb_version["is_anomaly"]
    )

    runs = _append_run_history(
        ROOT / "reports" / "run_history.jsonl",
        {
            "timestamp": now.isoformat(),
            "orders_rows": int(len(orders)),
            "contract_bad": orders_bad,
            "freshness_bad": freshness_bad,
            "kb_bad": kb_bad,
        },
    )

    contract_slo = calculate_slo(SLO_CONTRACT_TARGET, bad_events=int(orders_bad), total_events=1)
    slos = {
        "critical_contract_pass": {
            "short": _window_slo(runs, "contract_bad", SLO_CONTRACT_TARGET, SHORT_WINDOW_RUNS),
            "long": _window_slo(runs, "contract_bad", SLO_CONTRACT_TARGET, LONG_WINDOW_RUNS),
        },
        "revenue_freshness": {
            "short": _window_slo(runs, "freshness_bad", SLO_FRESHNESS_TARGET, SHORT_WINDOW_RUNS),
            "long": _window_slo(runs, "freshness_bad", SLO_FRESHNESS_TARGET, LONG_WINDOW_RUNS),
        },
        "rag_index_freshness": {
            "short": _window_slo(runs, "kb_bad", SLO_KB_TARGET, SHORT_WINDOW_RUNS),
            "long": _window_slo(runs, "kb_bad", SLO_KB_TARGET, LONG_WINDOW_RUNS),
        },
    }
    burns = {
        name: evaluate_multiwindow_burn(
            short_window_burn=windows["short"]["burn_rate"],
            long_window_burn=windows["long"]["burn_rate"],
            long_window_events=windows["long"]["total_events"],
        )
        for name, windows in slos.items()
    }

    paging = [name for name, burn in burns.items() if burn["page"]]

    # Status is driven by *customer impact*, not by which tool happened to fire.
    # A statistical signal with no rule behind it can still be a P1: losing 75%
    # of the orders makes the CEO dashboard wrong even though every contract
    # check passes.
    volume_collapse = bool(
        row_result["is_anomaly"]
        and row_result.get("direction") == "drop"
        and (row_result.get("relative_change") or 0) >= 0.30
    )
    incident_reasons = []
    if critical_failed:
        incident_reasons.append("critical orders-contract failure blocks the pipeline")
    if failed_issues(customer_issues, "critical"):
        incident_reasons.append(
            "customer dimension is broken; every join through it inflates "
            f"{', '.join(orders_blast['downstream_assets'])}"
        )
    if failed_issues(kb_issues, "critical") or kb_fresh["is_anomaly"] or kb_version["is_anomaly"]:
        incident_reasons.append("knowledge base is serving customers stale or rolled-back policy")
    if volume_collapse:
        incident_reasons.append(
            f"order volume collapsed {row_result.get('relative_change', 0):.0%} vs the "
            f"same-weekday baseline; {', '.join(orders_blast['downstream_assets'])} are wrong"
        )
    if paging:
        incident_reasons.append(f"burn-rate policy pages for {', '.join(paging)}")

    if incident_reasons:
        status = "INCIDENT"
    elif failed or kb_failed or customer_failed or row_result["is_anomaly"] or amount_result["is_anomaly"]:
        status = "DEGRADED"
    else:
        status = "HEALTHY"

    report = {
        "timestamp": now.isoformat(),
        "status": status,
        "incident_reasons": incident_reasons,
        # --- keys the starter dashboard/report already relied on ---
        "orders_rows": int(len(orders)),
        "failed_contract_checks": len(failed),
        "critical_contract_failures": len(critical_failed),
        "row_count_anomaly": row_result,
        "freshness_minutes": freshness_minutes,
        "kb_text_length_signal": text_result,
        "contract_slo": contract_slo,
        "sample_blast_radius_from_stg_orders": orders_blast["downstream_assets"],
        # --- added by this lab ---
        "contract_decision": decision,
        "contract_issues": [i for i in issues if not i["passed"]],
        "quarantined_rows": quarantined,
        "customers_contract_decision": customer_decision,
        "customers_contract_issues": [i for i in customer_issues if not i["passed"]],
        "kb_contract_decision": kb_decision,
        "kb_contract_issues": [i for i in kb_issues if not i["passed"]],
        "kb_freshness": kb_fresh,
        "kb_version_regression": kb_version,
        "amount_distribution": amount_result,
        "currency_distribution": currency_result,
        "status_distribution": status_result,
        "slos": slos,
        "burn_rate_decisions": burns,
        "paging_slos": paging,
        "blast_radius": {"orders": orders_blast, "knowledge_base": kb_blast},
        "run_history_size": len(runs),
    }
    out = ROOT / "reports" / "latest_metrics.json"
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    # ------------------------------------------------------------------ print
    print("=== DATA RELIABILITY BASELINE ===")
    print(f"status                   : {status}")
    for reason in incident_reasons:
        print(f"   >> {reason}")
    print(f"orders rows              : {len(orders)}")
    print(f"contract failed checks   : {len(failed)}")
    print(f"critical contract fails  : {len(critical_failed)}")
    print(f"contract decision        : {decision['action'].upper()} ({decision['reason']})")
    if quarantined:
        print(f"quarantined rows         : {quarantined}")
    for issue in failed:
        print(f"   ! {issue['severity']:<8} {issue['check']}({issue['column']}) -> "
              f"{issue['action']}: {issue['details'][:70]}")
    print(f"customers contract       : {len(customer_failed)} failed -> "
          f"{customer_decision['action'].upper()}")
    for issue in customer_failed:
        print(f"   ! {issue['severity']:<8} {issue['check']}({issue['column']}) -> "
              f"{issue['action']}: {issue['details'][:70]}")
    print(f"row-count anomaly        : {row_result['is_anomaly']} "
          f"({row_result['method']}, score={row_result['score']:.2f}, "
          f"{row_result.get('direction')}, expected~{row_result.get('expected', 0):.0f})")
    print(f"   baseline              : {row_result.get('baseline_source')}")
    print(f"amount distribution      : {amount_result['is_anomaly']} ({amount_result['reason'][:60]})")
    print(f"currency distribution    : {currency_result['is_anomaly']}")
    print(f"freshness minutes        : {freshness_minutes:.1f} "
          f"(threshold {FRESHNESS_THRESHOLD_MINUTES})")
    print(f"KB contract failures     : {len(kb_failed)} -> {kb_decision['action'].upper()}")
    print(f"KB freshness             : anomaly={kb_fresh['is_anomaly']} ({kb_fresh['reason'][:70]})")
    print(f"KB version regression    : {kb_version['is_anomaly']} ({kb_version['reason'][:70]})")
    print(f"KB length anomaly        : {text_result['is_anomaly']}")
    for name, burn in burns.items():
        window = slos[name]
        print(f"SLO {name:<24}: burn short={window['short']['burn_rate']:.1f}x "
              f"long={window['long']['burn_rate']:.1f}x -> {burn['alert_class'].upper()} "
              f"({burn['severity']})")
    print(f"blast radius (orders)    : {' -> '.join(orders_blast['downstream_assets'])}")
    print(f"impacted columns         : {orders_blast['impacted_columns']}")
    print(f"blast radius (kb)        : {' -> '.join(kb_blast['downstream_assets'])}")
    print(f"report                   : {out.relative_to(ROOT)}")
    return 2 if status == "INCIDENT" else 0


if __name__ == "__main__":
    raise SystemExit(main())
