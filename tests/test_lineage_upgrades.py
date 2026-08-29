"""Blast radius must be transitive, cycle-safe and column-aware."""
from pathlib import Path

from observability.lineage import (
    blast_radius,
    dbt_column_graph,
    extract_dbt_dataset_graph,
    get_column_downstream,
    get_downstream_assets,
    get_upstream_assets,
    load_column_graph,
    load_graph,
    to_openlineage_events,
)

ROOT = Path(__file__).resolve().parents[1]
GRAPH_FILE = ROOT / "data" / "baseline" / "lineage_graph.json"


def test_dataset_downstream_is_transitive_and_ordered():
    graph = {"raw": ["stg"], "stg": ["mart"], "mart": ["dashboard"]}
    assert get_downstream_assets(graph, "raw") == ["stg", "mart", "dashboard"]


def test_column_downstream_is_transitive():
    """The starter returned direct children only, so it under-reported the
    blast radius by two hops and the CEO dashboard looked unaffected."""
    columns = load_column_graph(GRAPH_FILE)
    assert get_column_downstream(columns, "raw_orders.amount") == [
        "stg_orders.amount_usd",
        "fct_daily_revenue.daily_revenue",
        "ceo_revenue_dashboard.revenue",
    ]


def test_kb_column_lineage_reaches_the_support_agent():
    columns = load_column_graph(GRAPH_FILE)
    assert "support_agent.answer" in get_column_downstream(columns, "kb_documents.content")


def test_traversal_is_cycle_safe():
    assert get_downstream_assets({"a": ["b"], "b": ["c"], "c": ["a"]}, "a") == ["b", "c"]


def test_unknown_node_has_no_downstream():
    assert get_downstream_assets(load_graph(GRAPH_FILE), "does_not_exist") == []


def test_upstream_finds_root_cause_candidates():
    graph = load_graph(GRAPH_FILE)
    assert set(get_upstream_assets(graph, "ceo_revenue_dashboard")) == {
        "fct_daily_revenue", "stg_orders", "stg_customers", "raw_orders", "raw_customers",
    }


def test_blast_radius_reports_depth_and_leaf_consumers():
    graph = load_graph(GRAPH_FILE)
    report = blast_radius(graph, "raw_orders", column_graph=load_column_graph(GRAPH_FILE))
    assert report["downstream_by_depth"]["1"] == ["stg_orders"]
    assert report["leaf_consumers"] == ["ceo_revenue_dashboard"]
    assert report["asset_count"] == 3


def test_dbt_manifest_graph_matches_the_hand_written_graph():
    manifest = ROOT / "dbt_project" / "target" / "manifest.json"
    if not manifest.exists():
        import pytest

        pytest.skip("run `make dbt` first")
    graph = extract_dbt_dataset_graph(manifest)
    assert get_downstream_assets(graph, "orders") == ["stg_orders", "fct_daily_revenue"]
    assert all(not node.startswith("test.") for node in graph)
    assert dbt_column_graph(manifest)


def test_openlineage_events_are_spec_shaped():
    events = to_openlineage_events({"a": ["b"]}, failed_assets=["a"])
    assert events[0]["eventType"] == "FAIL"
    assert events[0]["schemaURL"].startswith("https://openlineage.io/spec/")
    assert events[0]["inputs"][0]["name"] == "a"
    assert events[0]["outputs"][0]["name"] == "b"
