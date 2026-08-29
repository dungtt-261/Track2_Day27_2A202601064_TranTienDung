"""Incident-oriented reliability dashboard.

Designed around the question an on-call engineer asks at 3am, in order:
is something wrong -> what exactly -> who is affected -> do I have to act now.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "reports" / "latest_metrics.json"
HISTORY = ROOT / "data" / "history" / "metrics_history.csv"
RUN_HISTORY = ROOT / "reports" / "run_history.jsonl"

SLO_TARGETS = {
    "critical_contract_pass": 0.999,
    "revenue_freshness": 0.995,
    "rag_index_freshness": 0.99,
}
OWNERS = {
    "orders": "commerce-data",
    "knowledge_base": "support-ai",
}
STATUS_COLOR = {"HEALTHY": "🟢", "DEGRADED": "🟡", "INCIDENT": "🔴"}

st.set_page_config(page_title="Data Reliability Lab", layout="wide")

if not REPORT.exists():
    st.warning("Run `make baseline` first to generate reports/latest_metrics.json")
    st.stop()

report = json.loads(REPORT.read_text(encoding="utf-8"))
status = report.get("status", "UNKNOWN")

st.title(f"{STATUS_COLOR.get(status, '⚪')} Data Reliability — {status}")
st.caption(f"Last run: {report['timestamp']} · runbook: docs/SOLUTION.md#runbook")

for reason in report.get("incident_reasons", []):
    st.error(reason)

# --------------------------------------------------------------- headline row
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Orders rows", report["orders_rows"])
c2.metric("Freshness (min)", f"{report['freshness_minutes']:.1f}", delta="threshold 30", delta_color="off")
c3.metric("Contract failures", report["failed_contract_checks"])
c4.metric("Critical failures", report["critical_contract_failures"])
c5.metric("Quarantined rows", report.get("quarantined_rows", 0))

decision = report.get("contract_decision", {})
if decision:
    st.info(f"Pipeline decision: **{decision.get('action', '?').upper()}** — {decision.get('reason', '')}")

# --------------------------------------------------------------------- SLO row
st.subheader("SLO / error budget / burn rate")
slo_rows = []
for name, windows in report.get("slos", {}).items():
    burn = report.get("burn_rate_decisions", {}).get(name, {})
    slo_rows.append(
        {
            "SLO": name,
            "target": SLO_TARGETS.get(name),
            "SLI (long window)": round(windows["long"]["sli"], 4),
            "budget left": f"{windows['long']['remaining_error_budget_fraction']:.0%}",
            "burn short": round(windows["short"]["burn_rate"], 1),
            "burn long": round(windows["long"]["burn_rate"], 1),
            "alert": burn.get("alert_class", "-"),
            "severity": burn.get("severity", "-"),
            "why": burn.get("reason", "")[:90],
        }
    )
if slo_rows:
    st.dataframe(pd.DataFrame(slo_rows), width="stretch", hide_index=True)
    for name, burn in report.get("burn_rate_decisions", {}).items():
        if burn.get("page"):
            st.error(f"PAGE — {name}: {burn['reason']}")

# ---------------------------------------------------------------- failed rules
left, right = st.columns(2)
with left:
    st.subheader(f"Orders contract — owner `{OWNERS['orders']}`")
    issues = report.get("contract_issues", [])
    if issues:
        st.dataframe(
            pd.DataFrame(issues)[["check", "column", "severity", "action", "rows_affected", "details"]],
            width="stretch", hide_index=True,
        )
    else:
        st.success("All orders-contract checks passed")

with right:
    st.subheader(f"Knowledge base — owner `{OWNERS['knowledge_base']}`")
    kb_issues = report.get("kb_contract_issues", [])
    if kb_issues:
        st.dataframe(
            pd.DataFrame(kb_issues)[["check", "column", "severity", "action", "details"]],
            width="stretch", hide_index=True,
        )
    else:
        st.success("All knowledge-base contract checks passed")
    kb_fresh = report.get("kb_freshness", {})
    if kb_fresh:
        st.write(f"**Freshness:** {kb_fresh.get('reason', '')}")
    kb_version = report.get("kb_version_regression", {})
    if kb_version.get("is_anomaly"):
        st.error(f"Version regression: {kb_version.get('reason', '')}")

# --------------------------------------------------------------------- signals
st.subheader("Statistical signals")
signal_rows = []
for label, key in [
    ("row_count", "row_count_anomaly"),
    ("amount distribution", "amount_distribution"),
    ("currency mix", "currency_distribution"),
    ("status mix", "status_distribution"),
    ("kb chunk length", "kb_text_length_signal"),
]:
    signal = report.get(key, {})
    if not signal:
        continue
    signal_rows.append(
        {
            "signal": label,
            "anomaly": signal.get("is_anomaly"),
            "method": signal.get("method"),
            "score": round(float(signal.get("score", 0)), 2) if signal.get("score") is not None else None,
            "direction": signal.get("direction", "-"),
            "why": str(signal.get("reason", ""))[:100],
        }
    )
st.dataframe(pd.DataFrame(signal_rows), width="stretch", hide_index=True)

# --------------------------------------------------------------- blast radius
st.subheader("Blast radius")
for name, radius in report.get("blast_radius", {}).items():
    if not radius.get("downstream_assets"):
        continue
    st.write(f"**{name}** (`{radius['root']}`, owner `{OWNERS.get(name, '?')}`)")
    st.code(f"{radius['root']} -> " + " -> ".join(radius["downstream_assets"]), language="text")
    if radius.get("impacted_columns"):
        st.caption("Impacted columns")
        st.json(radius["impacted_columns"], expanded=False)

# -------------------------------------------------------------------- history
st.subheader("Historical row count (seasonality is real)")
history = pd.read_csv(HISTORY)
history["segment"] = history["day_of_week"].map(lambda d: "weekend" if d >= 5 else "weekday")
st.line_chart(history.set_index("date")[["row_count"]])
st.caption(
    "Weekend volume runs at ~43% of weekday volume, which is why the detector "
    "compares against same-weekday history instead of a flat 14-day mean."
)

if RUN_HISTORY.exists():
    runs = pd.read_json(RUN_HISTORY, lines=True)
    st.subheader("Reliability check history (burn-rate input)")
    st.dataframe(runs.tail(20), width="stretch", hide_index=True)
