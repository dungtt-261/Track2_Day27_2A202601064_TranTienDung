#!/usr/bin/env python3
"""Publish the lab's lineage and data-quality results to a real Marquez server.

    docker compose -f marquez/docker-compose.yml up -d
    python scripts/emit_lineage.py                    # emit + verify
    python scripts/emit_lineage.py --source dbt       # use target/manifest.json
    python scripts/emit_lineage.py --offline out.jsonl  # no server needed

The graph can come from the lab's `lineage_graph.json` or from a real dbt
`target/manifest.json`. Contract results from the latest reliability run travel
with the events as DataQualityAssertions facets, and any dataset the run marked
bad is emitted as a FAIL, so Marquez shows the incident, not just the topology.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from observability.lineage import (  # noqa: E402
    extract_dbt_dataset_graph,
    load_column_graph,
    load_graph,
    to_openlineage_events,
    write_openlineage_events,
)

GRAPH_FILE = ROOT / "data" / "baseline" / "lineage_graph.json"
MANIFEST = ROOT / "dbt_project" / "target" / "manifest.json"
METRICS = ROOT / "reports" / "latest_metrics.json"

OWNERS = {
    "raw_orders": "commerce-data",
    "raw_customers": "commerce-data",
    "stg_orders": "commerce-data",
    "stg_customers": "commerce-data",
    "fct_daily_revenue": "commerce-data",
    "ceo_revenue_dashboard": "exec-analytics",
    "kb_documents": "support-ai",
    "kb_active_docs": "support-ai",
    "rag_index": "support-ai",
    "support_agent": "support-ai",
}
DESCRIPTIONS = {
    "stg_orders": "Cleans and casts the raw order batch.",
    "fct_daily_revenue": "Daily completed-order revenue for the CEO dashboard.",
    "ceo_revenue_dashboard": "Executive revenue tile.",
    "rag_index": "Vector index backing the support agent.",
    "support_agent": "Customer-facing RAG agent.",
}


def _run_context() -> tuple[dict[str, list[dict[str, Any]]], list[str], dict[str, str]]:
    """Read the last reliability run: assertions, failed datasets, error text."""
    if not METRICS.exists():
        return {}, [], {}
    report = json.loads(METRICS.read_text(encoding="utf-8"))
    quality = {
        "raw_orders": report.get("contract_issues", []),
        "raw_customers": report.get("customers_contract_issues", []),
        "kb_documents": report.get("kb_contract_issues", []),
    }
    failed, errors = [], {}
    if report.get("critical_contract_failures") or report.get("contract_issues"):
        failed.append("raw_orders")
        errors["raw_orders"] = "; ".join(
            f"{i['check']}({i['column']}): {i['details']}" for i in report["contract_issues"][:3]
        ) or "orders contract failed"
    if report.get("customers_contract_issues"):
        failed.append("raw_customers")
        errors["raw_customers"] = "; ".join(
            f"{i['check']}({i['column']})" for i in report["customers_contract_issues"][:3]
        )
    row = report.get("row_count_anomaly", {})
    if row.get("is_anomaly"):
        failed.append("raw_orders")
        errors.setdefault(
            "raw_orders",
            f"row_count anomaly: {row.get('direction')} {row.get('relative_change', 0):.0%} "
            f"vs {row.get('baseline_source')}",
        )
    if report.get("kb_freshness", {}).get("is_anomaly") or report.get(
        "kb_version_regression", {}
    ).get("is_anomaly"):
        failed.append("kb_documents")
        errors["kb_documents"] = (
            report.get("kb_version_regression", {}).get("reason")
            or report.get("kb_freshness", {}).get("reason", "kb unhealthy")
        )
    return {k: v for k, v in quality.items() if v}, sorted(set(failed)), errors


def verify(url: str, namespace: str) -> int:
    """Read the graph back out of Marquez - emitting without verifying is faith."""
    datasets = requests.get(f"{url}/api/v1/namespaces/{namespace}/datasets", timeout=10).json()
    jobs = requests.get(f"{url}/api/v1/namespaces/{namespace}/jobs", timeout=10).json()
    print(f"\nMarquez now holds {len(datasets.get('datasets', []))} datasets "
          f"and {len(jobs.get('jobs', []))} jobs in namespace '{namespace}'")

    for job in jobs.get("jobs", []):
        latest = (job.get("latestRun") or {}).get("state", "-")
        marker = "FAIL" if latest == "FAILED" else latest
        print(f"  {job['name']:<32} last_run={marker}")

    column_lineage_found = 0
    for dataset in datasets.get("datasets", []):
        fields = (dataset.get("facets", {}).get("columnLineage") or {}).get("fields") or {}
        if fields:
            column_lineage_found += 1
            for column, info in fields.items():
                sources = ", ".join(
                    f"{i['name']}.{i['field']}" for i in info.get("inputFields", [])
                )
                print(f"  column lineage: {dataset['name']}.{column} <- {sources}")
    print(f"  datasets carrying column lineage : {column_lineage_found}")

    # Data-quality assertions ride on the *input dataset versions* of each run,
    # not on the dataset itself - a dataset is a thing, an assertion is an
    # observation made about it during one run.
    for job in jobs.get("jobs", []):
        run_id = (job.get("latestRun") or {}).get("id")
        if not run_id:
            continue
        run = requests.get(f"{url}/api/v1/jobs/runs/{run_id}", timeout=10).json()
        for input_version in run.get("inputDatasetVersions") or []:
            assertions = (
                input_version.get("facets", {}).get("dataQualityAssertions", {})
            ).get("assertions", [])
            for assertion in assertions:
                if assertion.get("success"):
                    continue
                name = input_version["datasetVersionId"]["name"]
                print(
                    f"  assertion FAILED on {name}: {assertion['severity']} "
                    f"{assertion['assertion']}({assertion.get('column')})"
                )

    for name in ("ceo_revenue_dashboard", "support_agent"):
        resp = requests.get(
            f"{url}/api/v1/lineage",
            params={"nodeId": f"dataset:{namespace}:{name}", "depth": 10},
            timeout=10,
        )
        if resp.ok:
            nodes = resp.json().get("graph", [])
            print(f"  lineage graph around {name}: {len(nodes)} nodes")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:5000", help="Marquez base URL")
    parser.add_argument("--namespace", default="data-reliability-lab")
    parser.add_argument("--source", choices=["lab", "dbt"], default="lab")
    parser.add_argument("--offline", metavar="PATH", help="write JSONL instead of POSTing")
    args = parser.parse_args()

    if args.source == "dbt":
        if not MANIFEST.exists():
            raise SystemExit("dbt manifest not found - run `make dbt` first")
        graph = extract_dbt_dataset_graph(MANIFEST)
    else:
        graph = load_graph(GRAPH_FILE)
    column_graph = load_column_graph(GRAPH_FILE)
    quality, failed, errors = _run_context()

    if args.offline:
        path = write_openlineage_events(
            args.offline, to_openlineage_events(graph, failed_assets=failed)
        )
        print(f"Wrote offline OpenLineage events to {path}")
        return 0

    from observability.openlineage_emitter import build_client, emit_graph

    try:
        requests.get(f"{args.url}/api/v1/namespaces", timeout=5).raise_for_status()
    except Exception as exc:
        raise SystemExit(
            f"Marquez is not reachable at {args.url}. Start it with:\n"
            f"  docker compose -f marquez/docker-compose.yml up -d\n({exc})"
        ) from exc

    summary = emit_graph(
        graph,
        client=build_client(args.url),
        namespace=args.namespace,
        column_graph=column_graph,
        owners=OWNERS,
        descriptions=DESCRIPTIONS,
        quality_issues=quality,
        failed_assets=failed,
        error_messages=errors,
    )
    print(f"Emitted {summary['events_sent']} OpenLineage events for "
          f"{len(summary['jobs'])} jobs -> {args.url}")
    if summary["failed_runs"]:
        print(f"  runs marked FAIL: {', '.join(summary['failed_runs'])}")
    if quality:
        print(f"  data-quality assertions attached for: {', '.join(quality)}")
    return verify(args.url, args.namespace)


if __name__ == "__main__":
    raise SystemExit(main())
