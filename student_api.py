"""Stable interface used by public and instructor-side hidden evaluation.

Internals were refactored during the lab (robust anomaly detection, contract
type/freshness validation, multi-window burn-rate policy, transitive column
lineage, embedding-drift signals) but every function below keeps the name,
positional signature and return shape documented in ``docs/STUDENT_API.md``.
Optional keyword arguments are additive and always have safe defaults.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from observability.anomaly import detect_anomaly
from observability.distribution import detect_distribution_shift
from observability.lineage import get_column_downstream, get_downstream_assets
from observability.rag_metrics import detect_embedding_norm_shift, detect_text_length_shift
from observability.slo import calculate_slo, evaluate_multiwindow_burn
from src.contract_validator import (
    decide_action,
    failed_issues,
    load_contract,
    split_quarantine,
    validate_dataframe,
)


def validate_orders(
    df: pd.DataFrame, contract_path: str | Path, **kwargs: Any
) -> list[dict[str, Any]]:
    """Validate an order batch against a data contract.

    Returns one dict per check with ``check``, ``column``, ``severity``,
    ``passed`` and ``details`` (plus ``action`` / ``rows_affected``).
    """
    return validate_dataframe(df, load_contract(contract_path), **kwargs)


def detect_metric(
    current: float,
    history: Iterable[float],
    *,
    method: str = "auto",
    context: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    return detect_anomaly(current, history, method=method, context=context, **kwargs)


def detect_distribution(
    current_values: Iterable[Any], baseline_values: Iterable[Any], **kwargs: Any
) -> dict[str, Any]:
    return detect_distribution_shift(current_values, baseline_values, **kwargs)


def slo_status(target: float, bad_events: int, total_events: int) -> dict[str, Any]:
    return calculate_slo(target, bad_events, total_events)


def multiwindow_burn(
    short_window_burn: float, long_window_burn: float, **kwargs: Any
) -> dict[str, Any]:
    return evaluate_multiwindow_burn(
        short_window_burn=short_window_burn,
        long_window_burn=long_window_burn,
        **kwargs,
    )


def downstream_assets(graph: dict[str, list[str]], start: str) -> list[str]:
    return get_downstream_assets(graph, start)


def column_downstream(graph: dict[str, list[str]], start: str) -> list[str]:
    return get_column_downstream(graph, start)


def rag_length_shift(
    current_texts: Iterable[str], baseline_batch_means: Iterable[float], **kwargs: Any
) -> dict[str, Any]:
    return detect_text_length_shift(current_texts, baseline_batch_means, **kwargs)


def rag_embedding_shift(
    current_norms: Iterable[float], baseline_norms: Iterable[float], **kwargs: Any
) -> dict[str, Any]:
    return detect_embedding_norm_shift(current_norms, baseline_norms, **kwargs)


# --------------------------------------------------------------------------- #
# Additive helpers (not required by the hidden evaluation, used by the pipeline)
# --------------------------------------------------------------------------- #
def contract_decision(issues: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold contract issues into one action: block / quarantine / warn / allow."""
    return decide_action(issues)


def quarantine(df: pd.DataFrame, contract_path: str | Path):
    """Split a batch into ``(clean_rows, quarantined_rows)``."""
    return split_quarantine(df, load_contract(contract_path))


def failed_checks(issues: list[dict[str, Any]], min_severity: str | None = None):
    return failed_issues(issues, min_severity)
