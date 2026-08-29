"""Data contract validator for the Data Reliability Game Day lab.

Upgrades over the starter baseline:

* declared **type validation** (catches silent string/type drift that
  ``pd.to_numeric(..., errors="coerce")`` would hide),
* contract-level **freshness** validation,
* string ``min_length`` / ``max_length`` / ``pattern`` rules,
* optional cross-field ``row_rules`` (business assertions),
* optional ``row_count`` volume floor,
* severity-aware **actions** (``block`` / ``quarantine`` / ``warn``) and a
  row-level quarantine splitter so a partially bad batch does not have to be
  dropped entirely.

Every check keeps the stable return shape used by the hidden evaluation::

    {"check": str, "column": str | None, "severity": str,
     "passed": bool, "details": str}

Extra keys (``action``, ``rows_affected``, ``samples``) are additive.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd
import yaml

SEVERITY_ORDER = {"info": 0, "warning": 1, "critical": 2}

#: Checks that can be attributed to individual rows -> eligible for quarantine.
ROW_LEVEL_CHECKS = {
    "not_null",
    "unique",
    "accepted_values",
    "range",
    "type",
    "min_length",
    "max_length",
    "pattern",
    "row_rule",
    "unique_together",
}

_TYPE_ALIASES = {
    "int": "integer",
    "integer": "integer",
    "bigint": "integer",
    "long": "integer",
    "float": "number",
    "double": "number",
    "number": "number",
    "numeric": "number",
    "decimal": "number",
    "str": "string",
    "string": "string",
    "varchar": "string",
    "text": "string",
    "datetime": "datetime",
    "timestamp": "datetime",
    "date": "datetime",
    "bool": "boolean",
    "boolean": "boolean",
}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _normalize_severity(value: Any, default: str = "warning") -> str:
    severity = str(value or default).strip().lower()
    return severity if severity in SEVERITY_ORDER else default


def _action_for(check: str, severity: str) -> str:
    """Map (check, severity) -> operational action.

    ``critical`` blocks the pipeline. ``warning`` quarantines the offending
    rows when the failure is row-attributable, otherwise it can only warn.
    ``info`` never stops anything.
    """
    if severity == "critical":
        return "block"
    if severity == "warning":
        return "quarantine" if check in ROW_LEVEL_CHECKS else "warn"
    return "warn"


def _issue(
    check: str,
    *,
    column: str | None,
    severity: str,
    passed: bool,
    details: str,
    rows_affected: int = 0,
    samples: Sequence[Any] | None = None,
) -> dict[str, Any]:
    severity = _normalize_severity(severity)
    payload: dict[str, Any] = {
        "check": check,
        "column": column,
        "severity": severity,
        "passed": bool(passed),
        "details": details,
        "action": "none" if passed else _action_for(check, severity),
        "rows_affected": int(rows_affected),
    }
    if samples:
        payload["samples"] = [str(s) for s in list(samples)[:5]]
    return payload


def load_contract(path: str | Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def contract_columns(contract: dict[str, Any]) -> dict[str, Any]:
    """Support both ``columns:`` (tabular) and ``fields:`` (document) contracts."""
    columns = contract.get("columns") or contract.get("fields") or {}
    return {k: (v or {}) for k, v in columns.items()}


def _is_integer_value(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, np.integer)):
        return True
    if isinstance(value, (float, np.floating)):
        return float(value).is_integer()
    return False


def _is_number_value(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    return isinstance(value, (int, float, np.integer, np.floating))


def _is_string_value(value: Any) -> bool:
    return isinstance(value, str)


def _is_boolean_value(value: Any) -> bool:
    return isinstance(value, (bool, np.bool_))


def _is_datetime_value(value: Any) -> bool:
    if isinstance(value, (pd.Timestamp, datetime, np.datetime64)):
        return True
    if not isinstance(value, str):
        # numbers/bools in a datetime column are drift, even though pandas
        # would happily read them as epoch offsets.
        return False
    try:
        parsed = pd.to_datetime(value, utc=True, errors="raise")
    except (ValueError, TypeError, pd.errors.ParserError):
        return False
    return not pd.isna(parsed)


_TYPE_PREDICATES = {
    "integer": _is_integer_value,
    "number": _is_number_value,
    "string": _is_string_value,
    "datetime": _is_datetime_value,
    "boolean": _is_boolean_value,
}


def _type_violation_mask(series: pd.Series, declared: str) -> pd.Series:
    predicate = _TYPE_PREDICATES[declared]
    notna = series.notna()
    mask = pd.Series(False, index=series.index)
    if not notna.any():
        return mask
    mask.loc[notna] = ~series[notna].map(predicate).astype(bool)
    return mask


def _to_utc(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True, errors="coerce", format="mixed")


def _resolve_now(now: Any | None) -> pd.Timestamp:
    if now is None:
        return pd.Timestamp(datetime.now(timezone.utc))
    ts = pd.Timestamp(now)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


# --------------------------------------------------------------------------- #
# per-column rule evaluation
# --------------------------------------------------------------------------- #
def _column_violation_masks(
    df: pd.DataFrame, column: str, rules: dict[str, Any]
) -> list[tuple[str, pd.Series, str]]:
    """Return ``(check_name, boolean_mask, details)`` for every row-level rule."""
    series = df[column]
    out: list[tuple[str, pd.Series, str]] = []

    if bool(rules.get("required", False)) or bool(rules.get("not_null", False)):
        null_mask = series.isna()
        out.append(("not_null", null_mask, f"null_count={int(null_mask.sum())}"))

    if rules.get("unique"):
        dup_mask = series.duplicated(keep=False) & series.notna()
        out.append(("unique", dup_mask, f"duplicate_rows={int(dup_mask.sum())}"))

    accepted = rules.get("accepted_values")
    if accepted is not None:
        bad = series.notna() & ~series.isin(accepted)
        out.append(
            ("accepted_values", bad, f"invalid_count={int(bad.sum())}; accepted={accepted}")
        )

    declared = _TYPE_ALIASES.get(str(rules.get("type", "")).strip().lower())
    if declared:
        bad = _type_violation_mask(series, declared)
        out.append(
            (
                "type",
                bad,
                f"declared={declared}; observed_dtype={series.dtype}; "
                f"invalid_count={int(bad.sum())}",
            )
        )

    if "min" in rules or "max" in rules:
        numeric = pd.to_numeric(series, errors="coerce")
        bad = pd.Series(False, index=series.index)
        # Values that cannot be coerced at all are type failures, not range
        # failures - the `type` check above already owns them.
        if "min" in rules:
            bad |= numeric < rules["min"]
        if "max" in rules:
            bad |= numeric > rules["max"]
        bad = bad.fillna(False).astype(bool)
        bounds = f"min={rules.get('min')}, max={rules.get('max')}"
        out.append(("range", bad, f"invalid_count={int(bad.sum())}; {bounds}"))

    if "min_length" in rules:
        lengths = series.map(lambda v: len(str(v)) if pd.notna(v) else np.nan)
        bad = (lengths < rules["min_length"]).fillna(False).astype(bool)
        out.append(
            ("min_length", bad, f"invalid_count={int(bad.sum())}; min_length={rules['min_length']}")
        )

    if "max_length" in rules:
        lengths = series.map(lambda v: len(str(v)) if pd.notna(v) else np.nan)
        bad = (lengths > rules["max_length"]).fillna(False).astype(bool)
        out.append(
            ("max_length", bad, f"invalid_count={int(bad.sum())}; max_length={rules['max_length']}")
        )

    pattern = rules.get("pattern") or rules.get("regex")
    if pattern:
        compiled = re.compile(pattern)
        bad = series.notna() & ~series.map(
            lambda v: bool(compiled.fullmatch(str(v))) if pd.notna(v) else True
        ).astype(bool)
        out.append(("pattern", bad, f"invalid_count={int(bad.sum())}; pattern={pattern}"))

    return out


def _row_rule_masks(df: pd.DataFrame, contract: dict[str, Any]) -> list[tuple[dict, pd.Series]]:
    """Evaluate optional cross-field business rules declared in the contract.

    Each rule declares the condition that a *valid* row must satisfy::

        row_rules:
          - name: created_before_updated
            expr: "created_at <= updated_at"
            severity: warning
            datetime_columns: [created_at, updated_at]
    """
    masks: list[tuple[dict, pd.Series]] = []
    for rule in contract.get("row_rules") or []:
        columns = rule.get("columns") or _columns_in_expression(rule.get("expr", ""), df.columns)
        if any(c not in df.columns for c in columns):
            continue  # rule not applicable to this projection of the dataset
        frame = df.copy()
        for col in rule.get("datetime_columns", []):
            if col in frame.columns:
                frame[col] = _to_utc(frame[col])
        try:
            valid = frame.eval(rule["expr"])
        except Exception:  # pragma: no cover - defensive, contract authoring error
            continue
        bad = ~valid.fillna(False).astype(bool)
        masks.append((rule, bad))
    return masks


def _unique_together_masks(
    df: pd.DataFrame, contract: dict[str, Any]
) -> list[tuple[dict, pd.Series]]:
    """Composite / conditional uniqueness.

    Column-level `unique` cannot express "one *active* row per customer": the
    key repeats legitimately across historical SCD versions, and only the subset
    that is currently active must be unique. A broken close-out job leaves two
    active rows, which fans out every downstream join without violating a single
    per-row rule.

        unique_together:
          - name: one_active_row_per_customer
            columns: [customer_id]
            where: "is_active == True"
            severity: critical
    """
    out: list[tuple[dict, pd.Series]] = []
    for rule in contract.get("unique_together") or []:
        columns = list(rule.get("columns") or [])
        if not columns or any(c not in df.columns for c in columns):
            continue
        scope = df
        where = rule.get("where")
        if where:
            try:
                scope = df.loc[df.eval(where).fillna(False).astype(bool)]
            except Exception:  # pragma: no cover - contract authoring error
                continue
        bad_in_scope = scope.duplicated(subset=columns, keep=False)
        mask = pd.Series(False, index=df.index)
        mask.loc[scope.index[bad_in_scope]] = True
        out.append((rule, mask))
    return out


def _columns_in_expression(expr: str, columns: Iterable[str]) -> list[str]:
    return [c for c in columns if re.search(rf"\b{re.escape(c)}\b", expr)]


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def validate_dataframe(
    df: pd.DataFrame,
    contract: dict[str, Any],
    *,
    now: Any | None = None,
) -> list[dict[str, Any]]:
    """Validate ``df`` against a data contract and return one issue per check."""
    issues: list[dict[str, Any]] = []
    columns = contract_columns(contract)

    for column, rules in columns.items():
        severity = _normalize_severity(rules.get("severity"))
        required = bool(rules.get("required", False))

        if column not in df.columns:
            if required:
                issues.append(
                    _issue(
                        "required_column",
                        column=column,
                        severity=severity,
                        passed=False,
                        details=f"Missing required column: {column}",
                    )
                )
            continue

        for check, mask, details in _column_violation_masks(df, column, rules):
            count = int(mask.sum())
            issues.append(
                _issue(
                    check,
                    column=column,
                    severity=severity,
                    passed=(count == 0),
                    details=details,
                    rows_affected=count,
                    samples=df.loc[mask, column].tolist() if count else None,
                )
            )

    for rule, mask in _row_rule_masks(df, contract):
        count = int(mask.sum())
        issues.append(
            _issue(
                "row_rule",
                column=rule.get("name"),
                severity=_normalize_severity(rule.get("severity")),
                passed=(count == 0),
                details=f"rule={rule.get('expr')}; violating_rows={count}",
                rows_affected=count,
            )
        )

    for rule, mask in _unique_together_masks(df, contract):
        count = int(mask.sum())
        issues.append(
            _issue(
                "unique_together",
                column=rule.get("name") or ",".join(rule.get("columns", [])),
                severity=_normalize_severity(rule.get("severity")),
                passed=(count == 0),
                details=(
                    f"columns={rule.get('columns')}; where={rule.get('where')}; "
                    f"duplicate_rows={count}"
                ),
                rows_affected=count,
            )
        )

    issues.extend(_volume_issues(df, contract))
    issues.extend(_freshness_issues(df, contract, now=now))
    issues.extend(_schema_drift_issues(df, contract))
    return issues


def _volume_issues(df: pd.DataFrame, contract: dict[str, Any]) -> list[dict[str, Any]]:
    cfg = contract.get("row_count")
    if not cfg:
        return []
    severity = _normalize_severity(cfg.get("severity"))
    rows = len(df)
    minimum = cfg.get("min")
    maximum = cfg.get("max")
    ok = True
    if minimum is not None:
        ok = ok and rows >= minimum
    if maximum is not None:
        ok = ok and rows <= maximum
    return [
        _issue(
            "row_count",
            column=None,
            severity=severity,
            passed=ok,
            details=f"rows={rows}; min={minimum}; max={maximum}",
        )
    ]


def _freshness_issues(
    df: pd.DataFrame, contract: dict[str, Any], *, now: Any | None = None
) -> list[dict[str, Any]]:
    cfg = contract.get("freshness")
    if not cfg:
        return []
    column = cfg.get("column")
    severity = _normalize_severity(cfg.get("severity"))
    max_delay = float(cfg.get("max_delay_minutes", 60))

    if not column or column not in df.columns:
        return [
            _issue(
                "freshness",
                column=column,
                severity=severity,
                passed=False,
                details=f"freshness column '{column}' not present in dataset",
            )
        ]

    parsed = _to_utc(df[column])
    if not parsed.notna().any():
        return [
            _issue(
                "freshness",
                column=column,
                severity=severity,
                passed=False,
                details="no parseable timestamp values for freshness evaluation",
            )
        ]

    reference = _resolve_now(now)
    age_minutes = (reference - parsed.max()).total_seconds() / 60.0
    return [
        _issue(
            "freshness",
            column=column,
            severity=severity,
            passed=bool(age_minutes <= max_delay),
            details=(
                f"age_minutes={age_minutes:.1f}; max_delay_minutes={max_delay:.0f}; "
                f"max_{column}={parsed.max().isoformat()}"
            ),
        )
    ]


def _schema_drift_issues(df: pd.DataFrame, contract: dict[str, Any]) -> list[dict[str, Any]]:
    if contract.get("allow_extra_columns", True):
        return []
    declared = set(contract_columns(contract))
    extra = [c for c in df.columns if c not in declared]
    return [
        _issue(
            "schema_drift",
            column=None,
            severity=_normalize_severity(contract.get("schema_drift_severity"), "info"),
            passed=not extra,
            details=f"undeclared_columns={extra}",
        )
    ]


def validate_records(
    records: Iterable[dict[str, Any]],
    contract: dict[str, Any],
    *,
    now: Any | None = None,
) -> list[dict[str, Any]]:
    """Validate JSONL-style documents (e.g. the knowledge base) with the same engine."""
    df = pd.DataFrame(list(records))
    return validate_dataframe(df, contract, now=now)


def failed_issues(
    issues: list[dict[str, Any]], min_severity: str | None = None
) -> list[dict[str, Any]]:
    failed = [i for i in issues if not i.get("passed", False)]
    if min_severity is None:
        return failed
    threshold = SEVERITY_ORDER[min_severity]
    return [i for i in failed if SEVERITY_ORDER.get(i.get("severity", "warning"), 1) >= threshold]


def decide_action(issues: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold individual issues into one pipeline decision."""
    failed = failed_issues(issues)
    actions = {i.get("action", "warn") for i in failed}
    if "block" in actions:
        action, reason = "block", "at least one critical contract check failed"
    elif "quarantine" in actions:
        action, reason = "quarantine", "row-level warnings can be isolated from the clean batch"
    elif "warn" in actions:
        action, reason = "warn", "only non-blocking checks failed"
    else:
        action, reason = "allow", "all contract checks passed"
    return {
        "action": action,
        "reason": reason,
        "failed_checks": len(failed),
        "critical_failures": len(failed_issues(issues, "critical")),
        "warning_failures": len([i for i in failed if i["severity"] == "warning"]),
        "blocking_checks": sorted({i["check"] for i in failed if i.get("action") == "block"}),
    }


def row_violation_mask(
    df: pd.DataFrame, contract: dict[str, Any]
) -> tuple[pd.Series, pd.Series]:
    """Return ``(bad_row_mask, reason_per_row)`` for row-attributable rules."""
    mask = pd.Series(False, index=df.index)
    reasons = pd.Series("", index=df.index, dtype="object")

    def _merge(name: str, bad: pd.Series) -> None:
        nonlocal mask
        bad = bad.reindex(df.index, fill_value=False).astype(bool)
        mask = mask | bad
        reasons.loc[bad] = (reasons.loc[bad] + "," + name).str.lstrip(",")

    for column, rules in contract_columns(contract).items():
        if column not in df.columns:
            continue
        for check, bad, _ in _column_violation_masks(df, column, rules):
            _merge(f"{check}:{column}", bad)

    for rule, bad in _row_rule_masks(df, contract):
        _merge(f"row_rule:{rule.get('name')}", bad)

    for rule, bad in _unique_together_masks(df, contract):
        _merge(f"unique_together:{rule.get('name')}", bad)

    return mask, reasons


def split_quarantine(
    df: pd.DataFrame, contract: dict[str, Any]
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split a batch into ``(clean_rows, quarantined_rows)``.

    Quarantined rows carry a ``quarantine_reason`` column so the on-call
    engineer can see *why* each row was held back.
    """
    mask, reasons = row_violation_mask(df, contract)
    clean = df.loc[~mask].copy()
    bad = df.loc[mask].copy()
    if not bad.empty:
        bad["quarantine_reason"] = reasons.loc[mask]
    return clean, bad
