"""OpenLineage emission.

Unit tests run anywhere: they capture events with a fake client instead of a
server. The integration test talks to a real Marquez and skips when one is not
running, so the suite stays green on a laptop without Docker.
"""
import os

import pytest
import requests

from observability.lineage import load_column_graph, load_graph, to_openlineage_events
from observability.openlineage_emitter import emit_graph

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GRAPH_FILE = ROOT / "data" / "baseline" / "lineage_graph.json"
MARQUEZ_URL = os.environ.get("MARQUEZ_URL", "http://localhost:5000")
NAMESPACE = "lab27-pytest"


class RecordingClient:
    """Stands in for OpenLineageClient and keeps the emitted RunEvents."""

    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


@pytest.fixture
def graph():
    return load_graph(GRAPH_FILE)


@pytest.fixture
def column_graph():
    return load_column_graph(GRAPH_FILE)


def test_one_job_per_transformation_node_not_per_edge(graph, column_graph):
    client = RecordingClient()
    summary = emit_graph(graph, client=client, column_graph=column_graph)
    # 7 nodes have at least one parent; START + COMPLETE for each.
    assert len(summary["jobs"]) == 7
    assert summary["events_sent"] == 14
    assert "build_fct_daily_revenue" in summary["jobs"]


def test_start_and_complete_are_paired_on_the_same_run_id(graph):
    client = RecordingClient()
    emit_graph(graph, client=client)
    by_run = {}
    for event in client.events:
        by_run.setdefault(event.run.runId, []).append(event.eventType.value)
    assert all(states == ["START", "COMPLETE"] for states in by_run.values())


def test_failed_asset_marks_the_downstream_run_as_fail(graph):
    client = RecordingClient()
    summary = emit_graph(graph, client=client, failed_assets=["raw_orders"])
    assert "stg_orders" in summary["failed_runs"]
    fail_events = [e for e in client.events if e.eventType.value == "FAIL"]
    assert fail_events
    assert "errorMessage" in fail_events[0].run.facets


def test_column_lineage_facet_points_at_the_real_source(graph, column_graph):
    client = RecordingClient()
    emit_graph(graph, client=client, column_graph=column_graph)
    outputs = [
        o
        for e in client.events
        for o in e.outputs
        if o.name == "fct_daily_revenue"
    ]
    facet = outputs[0].facets["columnLineage"]
    source = facet.fields["daily_revenue"].inputFields[0]
    assert (source.name, source.field) == ("stg_orders", "amount_usd")


def test_contract_results_ride_along_as_quality_assertions(graph):
    client = RecordingClient()
    issues = [
        {"check": "unique", "column": "order_id", "severity": "critical",
         "passed": False, "details": "duplicate_rows=6"}
    ]
    emit_graph(graph, client=client, quality_issues={"raw_orders": issues})
    inputs = [i for e in client.events for i in e.inputs if i.name == "raw_orders"]
    assertion = inputs[0].inputFacets["dataQualityAssertions"].assertions[0]
    assert (assertion.assertion, assertion.severity, assertion.success) == (
        "unique", "critical", False,
    )


def test_owner_travels_with_the_dataset(graph):
    client = RecordingClient()
    emit_graph(graph, client=client, owners={"raw_orders": "commerce-data"})
    inputs = [i for e in client.events for i in e.inputs if i.name == "raw_orders"]
    assert inputs[0].facets["ownership"].owners[0].name == "commerce-data"


def test_offline_events_are_still_spec_shaped(graph):
    """The dependency-free path used by `--offline` must stay valid."""
    events = to_openlineage_events(graph, failed_assets=["raw_orders"])
    assert events
    assert all(e["schemaURL"].startswith("https://openlineage.io/spec/") for e in events)
    assert any(e["eventType"] == "FAIL" for e in events)


# ------------------------------------------------------------------ integration
def _marquez_up() -> bool:
    try:
        return requests.get(f"{MARQUEZ_URL}/api/v1/namespaces", timeout=2).ok
    except Exception:
        return False


@pytest.mark.skipif(not _marquez_up(), reason="Marquez not running; see marquez/docker-compose.yml")
def test_events_land_in_a_real_marquez(graph, column_graph):
    from observability.openlineage_emitter import build_client

    emit_graph(
        graph,
        client=build_client(MARQUEZ_URL),
        namespace=NAMESPACE,
        column_graph=column_graph,
        owners={"raw_orders": "commerce-data"},
        failed_assets=["raw_orders"],
        error_messages={"raw_orders": "pytest integration check"},
    )
    datasets = requests.get(
        f"{MARQUEZ_URL}/api/v1/namespaces/{NAMESPACE}/datasets", timeout=10
    ).json()["datasets"]
    names = {d["name"] for d in datasets}
    assert {"raw_orders", "stg_orders", "fct_daily_revenue", "ceo_revenue_dashboard"} <= names

    revenue = next(d for d in datasets if d["name"] == "fct_daily_revenue")
    fields = revenue["facets"]["columnLineage"]["fields"]
    assert fields["daily_revenue"]["inputFields"][0]["field"] == "amount_usd"

    job = requests.get(
        f"{MARQUEZ_URL}/api/v1/namespaces/{NAMESPACE}/jobs/build_stg_orders", timeout=10
    ).json()
    assert job["latestRun"]["state"] == "FAILED"
