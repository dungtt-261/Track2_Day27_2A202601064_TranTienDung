# Lab Answers — questions asked in `docs/LAB_GUIDE.md`

## Phase 0 — Healthy baseline

**Which dataset is critical?**
`orders`. It is the only dataset on the path to a customer-visible number
(`ceo_revenue_dashboard.revenue`) with no human in the loop. `customers` is
critical *as a join key source*: it never appears in the output, but a duplicate
active row silently inflates revenue by 17.1% (measured). `kb_documents` is
critical for a different reason — it reaches customers directly through the
support agent, where a wrong answer is a compliance problem, not a chart.

**Which downstream consumers?**

```text
orders        -> stg_orders -> fct_daily_revenue -> ceo_revenue_dashboard
customers     -> stg_customers -> fct_daily_revenue (join key only)
kb_documents  -> kb_active_docs -> rag_index -> support_agent
```

Both chains terminate outside the data platform, which is what makes them P1
candidates.

**Which metric tells you the data is not trustworthy?**
No single one — that is the point of the lab. In order of how quickly they fire:

1. `critical_contract_failures` — deterministic, instant, zero false positives.
2. `row_count` anomaly vs the **same-weekday** baseline — catches completeness,
   which no per-row rule can express.
3. `kb_version_regression` and `kb_freshness` — catch a fresh-but-wrong index.
4. `burn_rate` over short and long windows — decides whether any of the above is
   worth waking someone up.

---

## Phase 1 — Contract

Type validation, freshness, severity and actions are implemented in
`src/contract_validator.py`. Severity maps to an action:
`critical → block`, `warning → quarantine` (when the failure is row-attributable)
or `warn`, `info → warn`.

`duplicate_pk` result: 1 critical failure (`unique(order_id)`, 6 duplicate rows)
→ decision `BLOCK`, 6 rows written to `data/quarantine/`, GX checkpoint exits 2.

---

## Phase 2 — dbt

**Why `not_null` / `unique` are not unit tests.**
A `not_null` test is a *data* test: it queries whatever rows happen to be in the
warehouse right now. It passes on an empty table, it passes on a lucky day, and
it says nothing about whether the SQL is correct — only about today's data. A
*unit test* feeds fixed input rows into the model and asserts the exact output,
so it tests the transformation logic itself, fails deterministically in CI, and
runs before any bad data exists.

The revenue-inflation bug proves the distinction: with a duplicated active
customer, every `not_null`, `unique` and `accepted_values` test still passes, the
model returns no error, and revenue is 17.1% too high. Only
`unit_tests.yml::revenue_not_inflated_by_duplicate_active_customer` catches it
(`daily_revenue 100.0 → 200.0` on a two-row fixture).

Added: 20 data tests (generic + 4 singular, including
`assert_revenue_matches_staging` and `assert_one_active_row_per_customer`) and
3 native unit tests.

---

## Phase 3 — Anomaly detection

**When is a z-score wrong?**

1. **Seasonality.** Weekend volume is ~43% of weekday volume. A flat window
   makes the detector choose between paging every Saturday and going blind.
2. **Contaminated history.** The window that yesterday's incident lives in has an
   inflated `std`, so today's incident scores *below* threshold — the masking
   effect. Measured: `detect_anomaly(400, [1000,1010,995,1008,300,1004,1012,998])`
   returns **no anomaly** under z-score, anomaly under MAD.
3. **Bimodal history.** Mixing weekdays and weekends inflates `std` so much that a
   75% collapse (252 → 63) scores below threshold and is missed entirely.
4. **Trend.** A growing metric drifts away from a stale mean and alerts daily.
5. **Zero variance.** `std == 0` makes the score undefined; the starter returned
   `mad_is_zero_todo` and never alerted, even on a metric going from 1000 to 0.
6. **Tight baselines.** 3σ of a metric that varies by 0.1% is still 0.3% — a real
   alert-fatigue source, handled with a minimum-relative-change guard.

`auto` handles all six. It is not ML: same-segment selection, median/MAD with an
explicit zero-MAD fallback chain, optional linear de-trending, an EWMA second
opinion, and suppression for announced events.

---

## Phase 4 — Lineage

**`stg_orders` breaks — what is affected?**

```python
>>> downstream_assets(load_graph("data/baseline/lineage_graph.json"), "stg_orders")
['fct_daily_revenue', 'ceo_revenue_dashboard']

>>> column_downstream(column_graph, "raw_orders.amount")
['stg_orders.amount_usd', 'fct_daily_revenue.daily_revenue', 'ceo_revenue_dashboard.revenue']
```

The starter's `get_column_downstream` returned only direct children, so it
reported one hop and made the dashboard look unaffected. Column lineage also
narrows the correction notice: only the revenue tile is wrong, not the whole
dashboard. `extract_dbt_dataset_graph` reproduces the same chain from a real
`target/manifest.json`.

**OpenLineage → Marquez (verified against a live server).** `make marquez-up &&
make lineage` publishes the graph through the official `openlineage-python`
client and reads it back:

```text
Marquez now holds 10 datasets and 7 jobs in namespace 'data-reliability-lab'
  build_stg_orders                 last_run=FAIL
  column lineage: fct_daily_revenue.daily_revenue <- stg_orders.amount_usd
  assertion FAILED on raw_orders: critical unique(order_id)
```

Contract results ride along as `DataQualityAssertions` facets and ownership as
`OwnershipDatasetFacet`, so the catalogue answers "what broke", "who is
affected" and "who to page" from one graph.

---

## Phase 5 — SLO / error budget

For SLO = 99.5% with 2 bad checks out of 100:

| Quantity | Value |
|---|---|
| actual bad rate | 2/100 = **0.02** |
| allowed bad rate | 1 − 0.995 = **0.005** |
| burn rate | 0.02 / 0.005 = **4.0x** |
| error budget for the window | 0.005 × 100 = 0.5 events |
| breached? | **yes** — 0.02 > 0.005, budget exhausted (0% remaining) |

At 4x burn, a 30-day budget is gone in 7.5 days. Under the multi-window policy
this is a **ticket**, not a page: it clears the 3x tier but not the 6x or 14.4x
page tiers.

`multiwindow_burn` implements the Google SRE tiers (14.4x/6x/3x/1x). Both windows
must agree: a transient spike (short 30x, long 0.4x) does not page, a sustained
burn (short 20x, long 18x) does, and an incident that already recovered (short
0.2x, long 15x) leaves a ticket instead of a page.

---

## Phase 6 — Which layer catches which failure

Measured, not predicted — each row is one real run:

| Fault | Contract | GX | dbt | Anomaly | KB signals | Result |
|---|---|---|---|---|---|---|
| `duplicate_pk` | **BLOCK** (critical) | **BLOCK** exit 2 | would fail `unique` | no | no | caught deterministically, 6 rows quarantined |
| `volume_drop` | pass | pass | **PASS 28/28** | **75% drop, score 10.23** | no | only the statistical layer sees it |
| `stale_kb` | pass | pass | pass | no | **freshness 190 min** | KB contract critical → BLOCK |
| `policy_rollback` | pass | pass | pass | no | **v4 → v3 regression** | fresh timestamp, obsolete content |
| `scd_break` | **BLOCK** via `unique_together` (31 rows) | pass | **`assert_one_active_row_per_customer` fails (15)** | no | no | invisible to every per-row rule; would inflate revenue 17.1% |

The table is the argument for defence in depth: no single layer catches more
than two of the five faults.

`scd_break` is the clearest case. It was first caught only by dbt, three steps
downstream of where the bad data landed. Adding a conditional-uniqueness rule
(`unique_together` with `where: is_active == True`) moved detection to the
landing zone, where it blocks instead of merely reporting — a plain column-level
`unique` cannot express it, because the key legitimately repeats across SCD
versions.

## Operational questions the lab asks you to keep asking

- **Who is impacted?** Resolved from lineage, not from guesswork — and at column
  granularity, so the correction notice is narrow enough to be credible.
- **Block or warn?** Driven by contract severity, not by which tool fired.
  Critical blocks; warnings quarantine the offending rows and let the clean
  batch through.
- **Is the alert actionable?** Every detector returns `reason`, `expected`,
  `direction` and `baseline_source`, so the page says *what* changed and *against
  what*, not just "anomaly=True".
- **Which false positives are likely?** Weekends (fixed with same-weekday
  baselines), tiny deviations on tight baselines (relative-change guard),
  announced events (`known_event` suppression), small-sample PSI (KS
  significance gate), and single-check burn spikes (window significance guard).
- **If this detector did not exist, what else would catch it?** For
  `volume_drop`: nothing in the current stack — which is why the top prevention
  item is publishing an expected row count with every batch.
