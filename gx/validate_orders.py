#!/usr/bin/env python3
"""Great Expectations Core 1.21 validation flow driven by the data contract.

The starter ran four loose expectations through ``batch.validate``. This version
is the real GX object model:

    contract YAML -> ExpectationSuite -> ValidationDefinition -> Checkpoint
                  -> severity-aware Actions -> exit code / quarantine decision

The Expectation Suite is **generated from** ``contracts/orders_contract.yaml``
so the contract stays the single source of truth: adding a rule to the YAML adds
it to GX, to the Python validator and to the pipeline gate at the same time.

Actions implemented:

* ``SeverityRoutingAction`` - maps GX severity to the operational decision
  (``block`` / ``quarantine`` / ``warn``) and prints an actionable summary.
* ``JsonReportAction``     - writes ``reports/gx_validation_result.json`` for the
  dashboard and the incident report.
* ``QuarantineAction``     - writes the offending rows to ``data/quarantine/``.

Exit code is non-zero when the batch must be blocked, so this can be wired into
CI or an Airflow task without further glue.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    import great_expectations as gx
    from great_expectations.checkpoint.actions import ValidationAction
    from great_expectations.core.run_identifier import RunIdentifier
except ImportError as exc:  # friendlier classroom failure
    raise SystemExit("great_expectations is not installed. Run: pip install -r requirements.txt") from exc

from src.contract_validator import load_contract, split_quarantine  # noqa: E402

SEVERITY_ACTION = {"critical": "block", "warning": "quarantine", "info": "warn"}


# --------------------------------------------------------------------------- #
# contract -> expectation suite
# --------------------------------------------------------------------------- #
def build_suite_from_contract(contract: dict[str, Any], name: str) -> gx.ExpectationSuite:
    """Translate the YAML contract into GX expectations, severity included."""
    suite = gx.ExpectationSuite(name=name)
    columns = contract.get("columns") or contract.get("fields") or {}

    suite.add_expectation(
        gx.expectations.ExpectTableColumnsToMatchSet(
            column_set=list(columns), exact_match=False, severity="critical"
        )
    )

    for column, rules in columns.items():
        rules = rules or {}
        severity = str(rules.get("severity", "warning")).lower()
        if rules.get("required"):
            suite.add_expectation(
                gx.expectations.ExpectColumnValuesToNotBeNull(column=column, severity=severity)
            )
        if rules.get("unique"):
            suite.add_expectation(
                gx.expectations.ExpectColumnValuesToBeUnique(column=column, severity=severity)
            )
        if rules.get("accepted_values") is not None:
            suite.add_expectation(
                gx.expectations.ExpectColumnValuesToBeInSet(
                    column=column, value_set=list(rules["accepted_values"]), severity=severity
                )
            )
        if "min" in rules or "max" in rules:
            suite.add_expectation(
                gx.expectations.ExpectColumnValuesToBeBetween(
                    column=column,
                    min_value=rules.get("min"),
                    max_value=rules.get("max"),
                    severity=severity,
                )
            )
        declared = str(rules.get("type", "")).lower()
        if declared in {"integer", "int", "bigint"}:
            suite.add_expectation(
                gx.expectations.ExpectColumnValuesToBeInTypeList(
                    column=column, type_list=["int", "int8", "int16", "int32", "int64", "Int64"],
                    severity=severity,
                )
            )
        elif declared in {"number", "float", "double", "numeric"}:
            suite.add_expectation(
                gx.expectations.ExpectColumnValuesToBeInTypeList(
                    column=column,
                    type_list=["float", "float32", "float64", "int", "int64", "Int64"],
                    severity=severity,
                )
            )
        elif declared in {"datetime", "timestamp", "date"}:
            # Type drift guard: values must actually parse as timestamps.
            suite.add_expectation(
                gx.expectations.ExpectColumnValuesToMatchStrftimeFormat(
                    column=column, strftime_format="%Y-%m-%dT%H:%M:%S%z", severity=severity
                )
            )
        elif declared in {"string", "str", "varchar", "text"}:
            suite.add_expectation(
                gx.expectations.ExpectColumnValuesToBeInTypeList(
                    column=column, type_list=["str", "object", "string"], severity=severity
                )
            )

    freshness = contract.get("freshness") or {}
    if freshness.get("column"):
        # GX cannot express "max(updated_at) is younger than N minutes" natively
        # for a pandas batch, so the freshness SLI stays in the Python validator
        # (src/contract_validator.py) and in the pipeline gate. Documented here
        # on purpose - knowing what a tool cannot do is part of the exercise.
        pass
    return suite


# --------------------------------------------------------------------------- #
# custom severity-aware actions
# --------------------------------------------------------------------------- #
def _failed_expectations(checkpoint_result) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for run_result in checkpoint_result.run_results.values():
        for result in run_result.results:
            if result.success:
                continue
            config = result.expectation_config
            severity = "warning"
            meta = getattr(config, "severity", None)
            if meta is not None:
                severity = str(getattr(meta, "value", meta)).lower()
            out.append(
                {
                    "expectation": config.type,
                    "column": (config.kwargs or {}).get("column"),
                    "severity": severity,
                    "action": SEVERITY_ACTION.get(severity, "warn"),
                    "unexpected_count": (result.result or {}).get("unexpected_count"),
                    "partial_unexpected_list": (result.result or {}).get(
                        "partial_unexpected_list", []
                    )[:5],
                }
            )
    return out


class SeverityRoutingAction(ValidationAction):
    """Turn GX severities into one pipeline decision."""

    type: Literal["severity_routing"] = "severity_routing"

    def run(self, checkpoint_result, action_context=None) -> dict:
        failures = _failed_expectations(checkpoint_result)
        actions = {f["action"] for f in failures}
        if "block" in actions:
            decision, exit_code = "block", 2
        elif "quarantine" in actions:
            decision, exit_code = "quarantine", 1
        elif "warn" in actions:
            decision, exit_code = "warn", 0
        else:
            decision, exit_code = "allow", 0

        print(f"\n[action:severity_routing] decision={decision.upper()} exit_code={exit_code}")
        for failure in failures:
            print(
                f"  - {failure['severity']:<8} {failure['expectation']}"
                f"({failure['column']}) -> {failure['action']}"
                f" unexpected={failure['unexpected_count']}"
            )
        return {"decision": decision, "exit_code": exit_code, "failures": failures}


class JsonReportAction(ValidationAction):
    """Persist a machine-readable result for the dashboard / incident report."""

    type: Literal["json_report"] = "json_report"
    output_path: str

    def run(self, checkpoint_result, action_context=None) -> dict:
        failures = _failed_expectations(checkpoint_result)
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "success": bool(checkpoint_result.success),
            "failed_expectations": failures,
            "critical_failures": [f for f in failures if f["severity"] == "critical"],
        }
        path = Path(self.output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        print(f"[action:json_report] wrote {path.relative_to(ROOT)}")
        return {"path": str(path)}


class QuarantineAction(ValidationAction):
    """Isolate offending rows so the clean part of the batch can still land."""

    type: Literal["quarantine"] = "quarantine"
    source_csv: str
    contract_path: str
    quarantine_dir: str

    def run(self, checkpoint_result, action_context=None) -> dict:
        if checkpoint_result.success:
            print("[action:quarantine] nothing to quarantine")
            return {"quarantined_rows": 0}
        df = pd.read_csv(self.source_csv)
        clean, bad = split_quarantine(df, load_contract(self.contract_path))
        out_dir = Path(self.quarantine_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = out_dir / f"orders_quarantine_{stamp}.csv"
        if not bad.empty:
            bad.to_csv(path, index=False)
        print(
            f"[action:quarantine] clean_rows={len(clean)} quarantined_rows={len(bad)}"
            + (f" -> {path.relative_to(ROOT)}" if not bad.empty else "")
        )
        return {"quarantined_rows": int(len(bad)), "clean_rows": int(len(clean))}


# --------------------------------------------------------------------------- #
def main() -> int:
    source_csv = ROOT / "data" / "incoming" / "orders.csv"
    contract_path = ROOT / "contracts" / "orders_contract.yaml"
    contract = load_contract(contract_path)
    df = pd.read_csv(source_csv)

    context = gx.get_context(mode="ephemeral")
    suite = context.suites.add(build_suite_from_contract(contract, name="orders_contract_suite"))

    data_source = context.data_sources.add_pandas("orders_pandas")
    asset = data_source.add_dataframe_asset(name="orders_dataframe")
    batch_definition = asset.add_batch_definition_whole_dataframe("whole_orders")

    validation_definition = context.validation_definitions.add(
        gx.ValidationDefinition(
            name="orders_contract_validation", data=batch_definition, suite=suite
        )
    )

    checkpoint = context.checkpoints.add(
        gx.Checkpoint(
            name="orders_contract_checkpoint",
            validation_definitions=[validation_definition],
            actions=[
                JsonReportAction(
                    name="json_report",
                    output_path=str(ROOT / "reports" / "gx_validation_result.json"),
                ),
                QuarantineAction(
                    name="quarantine",
                    source_csv=str(source_csv),
                    contract_path=str(contract_path),
                    quarantine_dir=str(ROOT / "data" / "quarantine"),
                ),
                SeverityRoutingAction(name="severity_routing"),
            ],
            result_format="COMPLETE",
        )
    )

    result = checkpoint.run(
        batch_parameters={"dataframe": df},
        run_id=RunIdentifier(run_name=f"orders-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"),
    )

    print(f"\nGX suite expectations : {len(suite.expectations)}")
    print(f"GX checkpoint success : {result.success}")
    failures = _failed_expectations(result)
    exit_code = 2 if any(f["severity"] == "critical" for f in failures) else 0
    print("GX gate               :", "BLOCK" if exit_code else "PASS")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
