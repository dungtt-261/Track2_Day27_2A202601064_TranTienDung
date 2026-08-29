# Solution — what was built and how to verify it

Student: **Trần Tiến Dũng** (2A202601064) · Lab 27, Track 2 · 2026-08-29

## Quick start

The lab needs **Python 3.10–3.13** (`dbt-core` and `great_expectations` both
require `<3.14`). This machine ships Python 3.14, so a conda environment is used:

```bash
conda create -y -n lab27 python=3.13
conda activate lab27
pip install -r requirements.txt

make reset          # healthy baseline, scaled to the current weekday
make baseline       # contracts + anomaly + lineage + SLO in one run
make tests-all      # 81 tests (10 public + 71 added)
make gx             # GX Suite -> ValidationDefinition -> Checkpoint -> Actions
make dbt            # 20 data tests + 3 unit tests
make dashboard      # Streamlit incident view
make marquez-up     # Marquez API :5000 + UI :3000 (needs Docker)
make lineage        # POST OpenLineage events to Marquez, then read them back
make incident       # full evidence bundle: GX + dbt + reliability run
make clean-faults   # restore healthy state
```

## What changed, by phase

### Phase 1 — Contract & validation (`src/contract_validator.py`)

| Added | Failure it catches |
|---|---|
| declared **type validation** | `amount = "1,234.00"` — `pd.to_numeric(errors="coerce")` turns it into NaN and the range check sees nothing |
| **freshness** with injectable `now` | a batch that stopped arriving |
| `min_length` / `max_length` / `pattern` | truncated KB content |
| cross-field **`row_rules`** | `updated_at` before `created_at` |
| conditional **`unique_together`** | two *active* rows per customer — a plain `unique` cannot express it, because the key repeats legitimately across SCD versions |
| optional `row_count` floor, `allow_extra_columns` | volume and schema drift (opt-in) |
| severity → **action** (`block`/`quarantine`/`warn`) | turns a check result into a pipeline decision |
| `split_quarantine` | isolates bad rows so the clean batch still lands |

`validate_records` runs the same engine over the JSONL knowledge base, so the
`fields:` contract in `contracts/kb_contract.yaml` is enforced too, and
`contracts/customers_contract.yaml` guards the dimension that feeds the revenue
join.

### Phase 1b — Great Expectations (`gx/validate_orders.py`)

The Expectation Suite is **generated from the contract YAML**, so the contract
stays the single source of truth. Full object model: Suite →
ValidationDefinition → Checkpoint → Actions, with three custom actions —
`JsonReportAction`, `QuarantineAction`, `SeverityRoutingAction` — and a non-zero
exit code when the batch must be blocked. GX's native `severity` field is used,
so `critical` failures block and `warning` failures quarantine.

Documented limitation: GX cannot express "max(updated_at) is younger than N
minutes" for a pandas batch, so the freshness SLI stays in the Python validator.

### Phase 2 — dbt (`dbt_project/`)

* **3 native unit tests**, including
  `revenue_not_inflated_by_duplicate_active_customer` — the smallest fixture that
  exposes the fan-out bug (`daily_revenue 100.0 → 200.0`).
* **4 singular tests**: `assert_nonnegative_revenue` (kept),
  `assert_one_active_row_per_customer`, `assert_revenue_matches_staging`
  (business test: the mart must reproduce the staging total),
  `assert_orders_are_fresh` (warn-severity).
* **Generic tests**: `relationships`, grain `unique` on `order_date`, plus a
  package-free custom generic test for strictly positive counts.
* **Model fixed**: `fct_daily_revenue` deduplicates the active dimension with
  `qualify row_number()`. Measured impact on the `scd_break` scenario —
  before: $9,635.81, after: $8,230.12 (**+17.1% inflation removed**).

### Phase 3 — Anomaly detection (`observability/anomaly.py`)

`auto` is now: same-segment (weekday) baseline selection → optional linear
de-trending → median/MAD with a real zero-MAD fallback chain → minimum-relative-
change guard → EWMA second opinion → `known_event` suppression. `zscore`, `mad`,
`ewma` and `iqr` remain callable individually.

`observability/distribution.py` replaces the mean ratio with PSI (effect size) +
two-sample KS (significance) + a robust standardised level shift, and handles
categorical mixes. Measured false-positive rate on identical distributions:
**0–3 out of 60** across n = 20…300, versus 38/40 for PSI alone.

### Phase 4 — Lineage (`observability/lineage.py`)

Transitive **column** lineage (the starter returned direct children only),
upstream traversal for root-cause candidates, depth-grouped blast radius with
leaf consumers and owners, a dbt `manifest.json` parser that drops test nodes,
best-effort dbt column lineage, and **OpenLineage emission to a real Marquez
server** (below). All traversals are cycle-safe.

### Phase 4b — OpenLineage → Marquez

Two emitters, on purpose:

* `observability/lineage.to_openlineage_events` — hand-built, spec-shaped JSON,
  no dependency. Used by `--offline` and by the tests, so lineage can still be
  produced on a machine without Docker.
* `observability/openlineage_emitter.py` — the **official `openlineage-python`
  client** over HTTP, emitting START + COMPLETE/FAIL per transformation node
  (one job per *edge* would be valid and unreadable).

Facets attached, and what each one buys during an incident:

| Facet | Why |
|---|---|
| `ColumnLineageDatasetFacet` | Marquez renders column lineage, so the notice says "only the revenue tile is wrong" instead of freezing a dashboard |
| `DataQualityAssertionsDatasetFacet` | every contract check (name, column, severity, pass/fail) travels with the dataset |
| `OwnershipDatasetFacet` | the graph itself says who to page |
| `ErrorMessageRunFacet` | a failed run carries the reason, not just a red dot |
| `DocumentationJobFacet`, `NominalTimeRunFacet` | context in the catalogue |

```bash
make marquez-up      # Postgres + Marquez API (:5000) + web UI (:3000)
make lineage         # emit the lab graph + latest contract results, then verify
make lineage-dbt     # same, graph parsed from dbt target/manifest.json
make marquez-down
```

Verified against a live server, not just constructed:

```text
Emitted 14 OpenLineage events for 7 jobs -> http://localhost:5000
  runs marked FAIL: stg_orders, stg_customers
  build_stg_orders                 last_run=FAIL
  column lineage: fct_daily_revenue.daily_revenue <- stg_orders.amount_usd
  column lineage: ceo_revenue_dashboard.revenue <- fct_daily_revenue.daily_revenue
  assertion FAILED on raw_orders: critical unique(order_id)
  assertion FAILED on raw_customers: critical unique_together(one_active_row_per_customer)
  lineage graph around ceo_revenue_dashboard: 10 nodes
```

`scripts/emit_lineage.py` reads the graph back out of Marquez after emitting —
an emitter that is never read back leaves a catalogue that looks healthy and is
stale.

### Phase 5 — SLO (`observability/slo.py`)

`calculate_slo` gains `sli`, `error_budget_events` and
`remaining_error_budget_events`. `evaluate_multiwindow_burn` implements the
Google SRE 14.4x / 6x / 3x / 1x tiers: both windows must agree before paging, a
recovered incident leaves a ticket, and a significance guard refuses to page
until the long window holds at least 5 checks. `rolling_burn` computes both
windows from a stream of check outcomes — `scripts/run_baseline.py` feeds it
`reports/run_history.jsonl`.

### Phase 6/7 — Pipeline, dashboard, reports

`scripts/run_baseline.py` is now a full triage run: contract → quarantine →
anomaly → distribution → KB freshness/version → lineage → three SLOs with burn
decisions → `HEALTHY` / `DEGRADED` / `INCIDENT` with explicit customer-impact
reasons, and a non-zero exit code on incidents. The dashboard shows status,
error budgets, burn windows, failed rules with owners, and the blast radius.

Two extra fault scenarios were added for practice: `scd_break` and
`policy_rollback` — both are "pipeline SUCCESS, numbers wrong" failures.

## Runbook

1. `make baseline` — read `status` and the `>>` impact lines first.
2. `status: INCIDENT` → check which layer fired:
   * critical contract failure → the batch is already blocked, rows are in
     `data/quarantine/`; fix the producer.
   * `row_count` anomaly with `direction: drop` → completeness problem; compare
     the landed `order_id` range against the batch maximum before blaming a filter.
   * `kb_version_regression` / `kb_freshness` → the support agent is answering
     from the wrong revision; disable the affected intent first.
3. Blast radius is in `reports/latest_metrics.json` → `blast_radius`. Use
   `impacted_columns` to keep the correction notice narrow.
4. `burn_rate_decisions` decides page vs ticket. `insufficient data` means wait
   for more checks, not "ignore".
5. Recover, then `make incident` and confirm `status: HEALTHY` with dbt
   `PASS=28 ERROR=0`.

## Two starter defects found and fixed

1. **The shipped healthy baseline contradicted its own history.**
   `generate_data.py` always writes the same number of orders, but
   `metrics_history.csv` models weekends at ~43% of weekday volume. On a weekend
   `make reset && make baseline` therefore started from a false INCIDENT.
   `reset_lab.py` now scales the healthy batch to the same-weekday median.
2. **A time-brittle public fixture.**
   `tests_public/test_contracts.py::healthy_df` hard-coded `2026-08-28`
   timestamps, so any freshness-aware validator — which Phase 1 requires — fails
   it forever. The fixture now builds timestamps relative to `now`; the test's
   intent is unchanged and is now actually true on any run date.

Both are recorded with reasoning in `reports/agent_log.md`.

## Stable API

`docs/STUDENT_API.md` is unchanged and honoured: all nine functions keep their
names, positional signatures and return shapes. New keyword arguments are
additive with safe defaults, so two-argument calls still behave as documented.
