"""Dataset- and column-level lineage, blast-radius reporting and OpenLineage export.

Blast radius is the question an incident commander actually asks: *who is
already serving wrong numbers because of this?* Dataset lineage answers it at
table granularity; column lineage narrows it to "only the revenue tile is
wrong, the order counts are fine", which is the difference between a full
dashboard freeze and a targeted correction notice.
"""
from __future__ import annotations

import json
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

Graph = Mapping[str, Iterable[str]]


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #
def load_graph(path: str | Path) -> dict[str, list[str]]:
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    return payload["dataset_lineage"] if "dataset_lineage" in payload else payload


def load_column_graph(path: str | Path) -> dict[str, list[str]]:
    with open(path, "r", encoding="utf-8") as f:
        payload = json.load(f)
    return payload.get("column_lineage", {})


def _children(graph: Graph, node: str) -> list[str]:
    value = graph.get(node, [])
    if isinstance(value, Mapping):  # tolerate {"children": [...]} style graphs
        value = value.get("children", value.get("downstream", []))
    if isinstance(value, str):
        return [value]
    return list(value or [])


def _bfs(graph: Graph, start: str) -> list[str]:
    """Transitive successors in BFS order, excluding ``start``. Cycle-safe."""
    seen = {start}
    queue: deque[str] = deque([start])
    out: list[str] = []
    while queue:
        node = queue.popleft()
        for child in _children(graph, node):
            if child not in seen:
                seen.add(child)
                out.append(child)
                queue.append(child)
    return out


# --------------------------------------------------------------------------- #
# traversal API
# --------------------------------------------------------------------------- #
def get_downstream_assets(graph: Graph, start: str) -> list[str]:
    """Return transitive downstream assets in BFS order, excluding ``start``."""
    return _bfs(graph, start)


def get_column_downstream(column_graph: Graph, start_column: str) -> list[str]:
    """Transitive column-level traversal (the starter returned direct children only)."""
    return _bfs(column_graph, start_column)


def reverse_graph(graph: Graph) -> dict[str, list[str]]:
    reversed_: dict[str, list[str]] = {}
    for parent in graph:
        for child in _children(graph, parent):
            reversed_.setdefault(child, []).append(parent)
    return reversed_


def get_upstream_assets(graph: Graph, start: str) -> list[str]:
    """Transitive upstream assets - the candidate root causes of a broken asset."""
    return _bfs(reverse_graph(graph), start)


def downstream_by_depth(graph: Graph, start: str) -> dict[int, list[str]]:
    """Group the blast radius by hop distance, so triage can prioritise."""
    levels: dict[int, list[str]] = {}
    seen = {start}
    frontier = [start]
    depth = 0
    while frontier:
        depth += 1
        nxt: list[str] = []
        for node in frontier:
            for child in _children(graph, node):
                if child not in seen:
                    seen.add(child)
                    nxt.append(child)
        if nxt:
            levels[depth] = nxt
        frontier = nxt
    return levels


def blast_radius(
    graph: Graph,
    start: str,
    *,
    column_graph: Graph | None = None,
    start_columns: Iterable[str] | None = None,
    metadata: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Full incident-ready blast-radius report for one broken asset."""
    assets = get_downstream_assets(graph, start)
    levels = downstream_by_depth(graph, start)
    metadata = metadata or {}
    columns: dict[str, list[str]] = {}
    if column_graph is not None:
        for col in start_columns or [c for c in column_graph if c.startswith(f"{start}.")]:
            columns[col] = get_column_downstream(column_graph, col)
    consumers = [a for a in assets if not _children(graph, a)]
    return {
        "root": start,
        "downstream_assets": assets,
        "downstream_by_depth": {str(k): v for k, v in levels.items()},
        "leaf_consumers": consumers,
        "impacted_columns": columns,
        "owners": {a: metadata.get(a, {}).get("owner") for a in assets if a in metadata},
        "asset_count": len(assets),
    }


# --------------------------------------------------------------------------- #
# dbt manifest
# --------------------------------------------------------------------------- #
_DBT_KEEP_PREFIXES = ("model.", "seed.", "source.", "snapshot.", "exposure.")


def extract_dbt_dataset_graph(
    manifest_path: str | Path, *, readable_names: bool = True
) -> dict[str, list[str]]:
    """Parse ``target/manifest.json`` into a dataset lineage graph.

    Tests are dropped (they are assertions, not data assets). With
    ``readable_names`` the dbt ``unique_id`` is reduced to the node name so the
    graph is comparable with the hand-written ``lineage_graph.json``.
    """
    path = Path(manifest_path)
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    def keep(uid: str) -> bool:
        return uid.startswith(_DBT_KEEP_PREFIXES)

    def name(uid: str) -> str:
        return uid.split(".")[-1] if readable_names else uid

    graph: dict[str, list[str]] = {}
    for parent, children in (manifest.get("child_map") or {}).items():
        if not keep(parent):
            continue
        kept = [name(c) for c in children if keep(c)]
        graph.setdefault(name(parent), [])
        graph[name(parent)].extend(c for c in kept if c not in graph[name(parent)])
    return graph


def dbt_column_graph(manifest_path: str | Path, *, catalog_path: str | Path | None = None) -> dict[str, list[str]]:
    """Best-effort column lineage from a dbt manifest.

    dbt-core does not emit column-level lineage, so we approximate: a column
    documented on a child model that shares its name (or the same name minus a
    common suffix) with a parent column is treated as derived from it. Good
    enough for blast radius, explicitly not a substitute for a SQL parser.
    """
    path = Path(manifest_path)
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    nodes = {**manifest.get("nodes", {}), **manifest.get("sources", {})}
    columns_by_node = {
        uid: set((node.get("columns") or {}).keys()) for uid, node in nodes.items()
    }
    graph: dict[str, list[str]] = {}
    for uid, node in nodes.items():
        child_name = uid.split(".")[-1]
        for parent_uid in node.get("depends_on", {}).get("nodes", []):
            parent_name = parent_uid.split(".")[-1]
            for column in columns_by_node.get(uid, set()):
                if column in columns_by_node.get(parent_uid, set()):
                    graph.setdefault(f"{parent_name}.{column}", [])
                    target = f"{child_name}.{column}"
                    if target not in graph[f"{parent_name}.{column}"]:
                        graph[f"{parent_name}.{column}"].append(target)
    return graph


# --------------------------------------------------------------------------- #
# OpenLineage export
# --------------------------------------------------------------------------- #
OPENLINEAGE_SCHEMA = "https://openlineage.io/spec/1-0-5/OpenLineage.json"


def to_openlineage_events(
    graph: Graph,
    *,
    job_namespace: str = "data-reliability-lab",
    producer: str = "https://github.com/lab27/data-reliability-game-day",
    run_id: str = "00000000-0000-0000-0000-000000000000",
    event_time: str | None = None,
    failed_assets: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Emit spec-shaped OpenLineage ``COMPLETE``/``FAIL`` RunEvents.

    Written by hand so the lab keeps its zero-extra-dependency promise; the JSON
    can be POSTed straight to a Marquez ``/api/v1/lineage`` endpoint.
    """
    event_time = event_time or datetime.now(timezone.utc).isoformat()
    failed = set(failed_assets)
    events: list[dict[str, Any]] = []
    for parent in graph:
        children = _children(graph, parent)
        if not children:
            continue
        for child in children:
            events.append(
                {
                    "eventType": "FAIL" if parent in failed or child in failed else "COMPLETE",
                    "eventTime": event_time,
                    "producer": producer,
                    "schemaURL": OPENLINEAGE_SCHEMA,
                    "run": {"runId": run_id},
                    "job": {"namespace": job_namespace, "name": f"{parent}->{child}"},
                    "inputs": [{"namespace": job_namespace, "name": parent}],
                    "outputs": [{"namespace": job_namespace, "name": child}],
                }
            )
    return events


def write_openlineage_events(path: str | Path, events: list[dict[str, Any]]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    return path
