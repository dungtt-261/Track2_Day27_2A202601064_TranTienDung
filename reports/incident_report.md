# Incident Report — INC-2026-08-29-01

**Title:** Revenue under-reported 75% and support agent serving withdrawn refund policy
**Date:** 2026-08-29 (lab game day)
**Commander:** Trần Tiến Dũng · **Scribe:** same · **Status:** Mitigated, verification complete

## Severity

**P1.**

Two customer-visible consequences at the same time:

* the CEO revenue dashboard understated completed revenue by **$6,513.57**
  (reported $1,450.07 against an expected ~$7,963.64 for the same weekday);
* the RAG support agent answered refund questions from **refund-policy v3**, a
  revision that had already been superseded by v4.

Neither the ingestion job, dbt, nor any pipeline task reported a failure. `dbt
build` finished **PASS=28 ERROR=0** during the incident window.

## Summary

At 15:53 UTC the orders batch landed with 63 rows instead of the ~252 expected
for a Saturday, and the knowledge-base index was rebuilt with an older revision
of the refund policy. Deterministic contract checks passed on both datasets — the
rows that *did* arrive were perfectly valid, and a document at version 3 violates
no rule that was written in advance. The failure was caught by the statistical and
version-comparison layers, escalated by the multi-window burn-rate policy, and the
blast radius was resolved through dataset and column lineage.

## Detection

| Signal | Layer | Value |
|---|---|---|
| `row_count` anomaly | robust MAD vs same-weekday baseline | score 10.23, **75% drop**, expected ~252 |
| `kb_version_regression` | KB version comparison | `refund-policy` v4 → **v3** |
| `kb_text_length_signal` | RAG chunk-length drift | mean 14.2 words vs baseline 16.1 |
| `critical_contract_pass` SLO | multi-window burn rate | short 1000x / long 461x → **PAGE** |
| `rag_index_freshness` SLO | multi-window burn rate | short 100x / long 46x → **PAGE** |

- **Signal that fired first:** `row_count` anomaly (`auto:mad`), immediately on
  the first run after the bad batch landed.
- **First observed:** first `make baseline` run after 15:53 UTC.
- **Time to page:** the burn-rate policy deliberately did *not* page on the first
  bad run. It paged once the long window contained enough checks to be
  significant — sustained burn, not a blip.

**What did NOT fire, and why that matters:**

| Layer | Result | Why |
|---|---|---|
| Orders data contract | PASS | Every row that arrived was valid. Missing rows violate no per-row rule. |
| Great Expectations checkpoint | PASS | Same reason — expectations describe rows, not the absence of rows. |
| dbt tests (28) | PASS | The mart is internally consistent with the truncated input. |
| KB data contract | PASS | v3 is a valid integer version. "Older than yesterday" is not expressible as a static rule. |
| KB freshness | PASS | The rollback re-published with a fresh timestamp — the index was *fresh and wrong*. |

## Root Cause

**Two independent faults in the same window.**

1. **Partial ingestion of the orders batch.** The surviving rows are a
   contiguous `order_id` prefix: `100000 … 100175`, with the batch maximum at
   `100599`. Every row is valid and the timestamp range is unbroken
   (12:27 → 15:41 UTC), so this is not a filter, a schema change or a late
   arrival. The signature of a truncated write is a producer that stopped early
   — the ingestion job terminated mid-batch and the downstream steps happily
   processed the partial file, because nothing downstream knew how many rows
   were supposed to arrive.

2. **Knowledge-base version rollback.** `refund-policy` moved from v4 to v3 with
   a *new* `published_at`. A re-index from a stale source snapshot behaves
   exactly this way: fresh publication metadata, obsolete content. Freshness
   monitoring cannot see it because the timestamp is genuinely new.

The deeper cause is the same for both: **the pipeline's success criteria only
described the rows that arrived, never the rows that should have arrived.**

## Evidence

1. `reports/latest_metrics.json` → `row_count_anomaly`:
   `{"is_anomaly": true, "method": "auto:mad", "score": 10.23, "direction": "drop", "expected": 252, "relative_change": 0.75, "baseline_source": "same_segment_history(n=6)"}`.
   The same-weekday baseline matters: against a mixed 14-day window the plain
   z-score scores this **below threshold and reports no anomaly**
   (`tests/test_anomaly_upgrades.py::test_mixed_history_blinds_the_plain_zscore`).
2. `order_id` range in the landed file is `100000…100175`, batch max is `100599`
   — a prefix, not a sample. Rules out filtering; points at a truncated write.
3. `created_at` of surviving rows spans 12:27→15:41 UTC with no gap, so the loss
   is not time-bounded — it is count-bounded. Same conclusion.
4. Contract validation returned **0 failed checks** on the same file, so the
   defect is in completeness, not in row validity.
5. `kb_version_regression` →
   `regressed_docs=[{"doc_id": "refund-policy", "current_version": 3, "baseline_version": 4}]`.
6. `kb_freshness` → `newest_document_age=10.3 min` (inside the 60-minute SLA).
   Fresh and wrong: this is why freshness alone is not a correctness signal.
7. Multi-window burn rate: `critical_contract_pass` short 1000x / long 461x →
   `page=True, severity=critical`. The first bad run did **not** page
   (`insufficient data: long window has 2 event(s)`), which is the intended
   behaviour.

## Blast Radius

Resolved from `data/baseline/lineage_graph.json` (dataset and column level):

```text
raw_orders (truncated)
  -> stg_orders
     -> fct_daily_revenue
        -> ceo_revenue_dashboard          [CUSTOMER-VISIBLE, owner: commerce-data]

column: raw_orders.amount
  -> stg_orders.amount_usd
     -> fct_daily_revenue.daily_revenue
        -> ceo_revenue_dashboard.revenue  [the wrong number the CEO saw]

kb_documents (rolled back)
  -> kb_active_docs
     -> rag_index
        -> support_agent                  [CUSTOMER-VISIBLE, owner: support-ai]
```

Column lineage narrows the correction notice: only the **revenue** tile is
wrong. Order-status and currency breakdowns derive from columns that are not
downstream of `amount`, so they did not need to be retracted.

Not impacted: `stg_customers`, and every consumer that does not descend from
`stg_orders` or `kb_documents`.

## Mitigation

1. Froze the CEO dashboard's revenue tile and notified `commerce-data`.
2. Disabled the support agent's refund intent and notified `support-ai`.
3. Re-ran ingestion for the full batch; re-published `refund-policy` v4.
4. Left the quarantine path in place: `make baseline` now writes offending rows
   to `data/quarantine/` instead of dropping or admitting the whole batch.

## Recovery

```bash
make clean-faults      # restore the full batch and the current KB revision
make incident          # GX checkpoint + dbt build + reliability run
```

Post-recovery run: `status: HEALTHY`, `row_count_anomaly: false`,
`kb_version_regression: false`, all burn rates 0.0x, dbt `PASS=28 ERROR=0`.

## Verification

- [x] Contract healthy — 0 failed checks, decision `ALLOW`
- [x] dbt tests healthy — 20 data tests + 3 unit tests, `PASS=28 ERROR=0`
- [x] anomaly returned to expected range — `row_count` score 0.03, no anomaly
- [x] SLO healthy / budget understood — all three SLOs at 0.0x burn, alert class `none`
- [x] downstream output verified — `fct_daily_revenue` equals the staging total
      (`tests/assert_revenue_matches_staging.sql` passes), KB back at v4

## Prevention / Action Items

| Action | Owner | Deadline | Why |
|---|---|---|---|
| Publish an expected row count with every batch and assert it on landing | commerce-data | 2026-09-05 | Completeness is the one property no per-row rule can express. Would have failed this batch deterministically in seconds. |
| Make the ingestion job write atomically (staging file → rename on success) | commerce-data | 2026-09-12 | A truncated write should never be visible downstream at all. |
| Assert monotonic `version` per `doc_id` at KB index build time | support-ai | 2026-09-05 | Version rollback is deterministic once the previous version is known; it should never depend on a statistical signal. |
| Keep the same-weekday anomaly baseline; alert on `direction=drop` for `row_count` | data-reliability | done | A flat baseline either pages every weekend or goes blind to real drops. |
| Keep the burn-rate significance guard (`min_long_window_events=5`) | data-reliability | done | Prevents a single failed check from paging at a nominal 1000x burn rate. |
| Add `assert_one_active_row_per_customer` to the blocking test set | commerce-data | done | Guards the fan-out that inflated revenue 17.1% in the `scd_break` scenario. |

## What we learned

`SUCCESS` from the pipeline meant "no exception was raised". Neither fault raises
one. The layers that caught them — same-weekday statistical baselines, version
comparison against a known-good snapshot, and lineage — are the ones that
describe the data's *expected shape and history*, not just each row in isolation.
