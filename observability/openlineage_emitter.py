"""Emit real OpenLineage events to a Marquez server.

`observability.lineage.to_openlineage_events` builds spec-shaped JSON with no
dependencies, which is enough to read and to diff. This module is the real
thing: it uses the official ``openlineage-python`` client over HTTP, so the
events are validated by the client and land in Marquez's own data model.

What is attached to each run, and why it is worth attaching:

* **ColumnLineageDatasetFacet** - Marquez then renders column-level lineage, so
  an incident notice can say "only the revenue tile is wrong" instead of
  freezing a whole dashboard.
* **DataQualityAssertionsDatasetFacet** - every contract check (name, column,
  severity, pass/fail) travels with the dataset, so the catalogue shows *why* a
  run failed next to the lineage that says *who is affected*.
* **OwnershipDatasetFacet / DocumentationJobFacet** - the on-call engineer gets
  a name to page straight from the graph.
* **ErrorMessageRunFacet** - failed runs carry the reason, not just a red dot.

One job is emitted per transformation node (inputs -> that node), which is the
model Marquez expects; emitting one job per *edge* would produce a graph that is
technically valid and useless to look at.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from openlineage.client import OpenLineageClient
from openlineage.client.event_v2 import (
    InputDataset,
    Job,
    OutputDataset,
    Run,
    RunEvent,
    RunState,
)
from openlineage.client.facet_v2 import (
    column_lineage_dataset,
    data_quality_assertions_dataset,
    documentation_job,
    error_message_run,
    nominal_time_run,
    ownership_dataset,
)
from openlineage.client.transport.http import HttpConfig, HttpTransport
from openlineage.client.uuid import generate_new_uuid

from observability.lineage import reverse_graph

DEFAULT_MARQUEZ_URL = os.environ.get("MARQUEZ_URL", "http://localhost:5000")
PRODUCER = "https://github.com/lab27/data-reliability-game-day"


def build_client(url: str = DEFAULT_MARQUEZ_URL, *, timeout: float = 10.0) -> OpenLineageClient:
    return OpenLineageClient(
        transport=HttpTransport(HttpConfig(url=url, endpoint="api/v1/lineage", timeout=timeout))
    )


def _column_lineage_facet(
    node: str, column_graph: Mapping[str, Any], namespace: str
) -> column_lineage_dataset.ColumnLineageDatasetFacet | None:
    """Invert the lab's `parent.column -> [child.column]` map for one dataset."""
    fields: dict[str, column_lineage_dataset.Fields] = {}
    for source, targets in column_graph.items():
        if "." not in source:
            continue
        src_dataset, src_column = source.rsplit(".", 1)
        for target in targets or []:
            if "." not in target:
                continue
            tgt_dataset, tgt_column = target.rsplit(".", 1)
            if tgt_dataset != node:
                continue
            entry = fields.setdefault(
                tgt_column,
                column_lineage_dataset.Fields(
                    inputFields=[],
                    transformationDescription=f"derived from {src_dataset}.{src_column}",
                    transformationType="DIRECT",
                ),
            )
            entry.inputFields.append(
                column_lineage_dataset.InputField(
                    namespace=namespace, name=src_dataset, field=src_column
                )
            )
    if not fields:
        return None
    return column_lineage_dataset.ColumnLineageDatasetFacet(fields=fields)


def _assertion_facet(
    issues: Iterable[Mapping[str, Any]]
) -> data_quality_assertions_dataset.DataQualityAssertionsDatasetFacet | None:
    assertions = [
        data_quality_assertions_dataset.Assertion(
            assertion=str(issue.get("check")),
            success=bool(issue.get("passed")),
            column=issue.get("column"),
            severity=str(issue.get("severity", "warning")),
            name=f"{issue.get('check')}:{issue.get('column')}",
            description=str(issue.get("details", ""))[:500],
        )
        for issue in issues
    ]
    if not assertions:
        return None
    return data_quality_assertions_dataset.DataQualityAssertionsDatasetFacet(assertions=assertions)


def emit_graph(
    graph: Mapping[str, Any],
    *,
    client: OpenLineageClient | None = None,
    namespace: str = "data-reliability-lab",
    job_namespace: str | None = None,
    column_graph: Mapping[str, Any] | None = None,
    owners: Mapping[str, str] | None = None,
    descriptions: Mapping[str, str] | None = None,
    quality_issues: Mapping[str, list[Mapping[str, Any]]] | None = None,
    failed_assets: Iterable[str] = (),
    error_messages: Mapping[str, str] | None = None,
    event_time: str | None = None,
) -> dict[str, Any]:
    """Emit START + COMPLETE/FAIL for every transformation node in ``graph``.

    Returns a summary: jobs emitted, events sent, and which runs were marked
    FAIL. Raises whatever the transport raises - a silent lineage emitter is
    worse than none, because the catalogue then looks healthy and stale.
    """
    client = client or build_client()
    job_namespace = job_namespace or namespace
    column_graph = column_graph or {}
    owners = owners or {}
    descriptions = descriptions or {}
    quality_issues = quality_issues or {}
    error_messages = error_messages or {}
    failed = set(failed_assets)
    event_time = event_time or datetime.now(timezone.utc).isoformat()

    parents_of = reverse_graph(graph)
    jobs: list[str] = []
    events_sent = 0
    failed_runs: list[str] = []

    def dataset_facets(name: str) -> dict[str, Any]:
        facets: dict[str, Any] = {}
        if name in owners:
            facets["ownership"] = ownership_dataset.OwnershipDatasetFacet(
                owners=[ownership_dataset.Owner(name=owners[name], type="TEAM")]
            )
        column_facet = _column_lineage_facet(name, column_graph, namespace)
        if column_facet is not None:
            facets["columnLineage"] = column_facet
        return facets

    for node, parents in parents_of.items():
        if not parents:
            continue
        run_id = str(generate_new_uuid())
        job = Job(
            namespace=job_namespace,
            name=f"build_{node}",
            facets={
                "documentation": documentation_job.DocumentationJobFacet(
                    description=descriptions.get(
                        node, f"Produces {node} from {', '.join(parents)}"
                    )
                )
            },
        )
        run_facets: dict[str, Any] = {
            "nominalTime": nominal_time_run.NominalTimeRunFacet(nominalStartTime=event_time)
        }

        inputs = []
        for parent in parents:
            input_facets = {}
            facet = _assertion_facet(quality_issues.get(parent, []))
            if facet is not None:
                input_facets["dataQualityAssertions"] = facet
            inputs.append(
                InputDataset(
                    namespace=namespace,
                    name=parent,
                    inputFacets=input_facets,
                    facets=dataset_facets(parent),
                )
            )
        outputs = [
            OutputDataset(namespace=namespace, name=node, facets=dataset_facets(node))
        ]

        client.emit(
            RunEvent(
                eventType=RunState.START,
                eventTime=event_time,
                producer=PRODUCER,
                run=Run(runId=run_id, facets=run_facets),
                job=job,
                inputs=inputs,
                outputs=outputs,
            )
        )
        events_sent += 1

        is_failed = node in failed or any(p in failed for p in parents)
        if is_failed:
            message = error_messages.get(node) or next(
                (error_messages[p] for p in parents if p in error_messages),
                f"upstream data quality failure affecting {node}",
            )
            run_facets = {
                **run_facets,
                "errorMessage": error_message_run.ErrorMessageRunFacet(
                    message=message, programmingLanguage="PYTHON"
                ),
            }
            failed_runs.append(node)

        client.emit(
            RunEvent(
                eventType=RunState.FAIL if is_failed else RunState.COMPLETE,
                eventTime=datetime.now(timezone.utc).isoformat(),
                producer=PRODUCER,
                run=Run(runId=run_id, facets=run_facets),
                job=job,
                inputs=inputs,
                outputs=outputs,
            )
        )
        events_sent += 1
        jobs.append(job.name)

    return {
        "namespace": namespace,
        "jobs": jobs,
        "events_sent": events_sent,
        "failed_runs": failed_runs,
    }
