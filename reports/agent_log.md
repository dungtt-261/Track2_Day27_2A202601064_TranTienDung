# AI Agent Decision Log

Agent: Claude Code (Opus 5). Every proposal below was run and checked against
evidence before being kept; three were rejected or revised.

---

## Decision 1 — Freshness validation vs. a time-brittle public test

- **Hypothesis:** the contract declares `freshness.max_delay_minutes: 30`, so a
  freshness check belongs in `validate_dataframe`.
- **Prompt:** "Add contract-level freshness validation with an injectable
  reference time."
- **Agent proposal:** evaluate `now - max(updated_at)` against the threshold.
- **Evidence:** implementing it broke
  `tests_public::test_healthy_contract_passes_starter_checks` — its fixture
  hard-codes `2026-08-28T10:00:00Z`, permanently more than 30 minutes old.
- **Decision:** **revise**. Kept the freshness check (Phase 1 requires it) and
  fixed the fixture to build timestamps relative to `now`. The test's intent —
  "healthy data passes every check" — is preserved and is now actually true on
  any run date; a fixture that is permanently stale cannot express it.
- **Why:** the fixture was the defect, not the check. Deleting the check to
  protect a brittle fixture would have traded a required capability for a green
  tick.

## Decision 2 — Seasonality: same-weekday baseline instead of a flat window

- **Hypothesis:** weekend volume is ~43% of weekday volume, so a flat 14-day
  baseline must either page every Saturday or go blind to real drops.
- **Prompt:** "Make `auto` context-aware using `same_segment_history` /
  `day_of_week`; keep the z-score function intact."
- **Agent proposal:** select the most specific usable window, then score with
  median/MAD.
- **Evidence:** `tests/test_anomaly_upgrades.py` — a healthy Saturday (252) is
  not flagged, a 75% drop on the *same* Saturday is (score 10.23). Against the
  mixed window the plain z-score scores the 63-row collapse **below threshold**
  and reports nothing at all: the bimodal history inflates `std` until the
  detector is blind.
- **Decision:** **accept.**

## Decision 3 — The shipped "healthy" baseline contradicted its own history

- **Hypothesis:** `make reset && make baseline` should report HEALTHY.
- **Evidence:** it reported INCIDENT. `generate_data.py` always writes 600
  orders regardless of weekday, but `metrics_history.csv` says Saturdays run at
  ~250. On a weekend the seasonality-aware detector correctly reported a 2.4x
  spike — on data the lab calls healthy.
- **Agent proposal (rejected):** relax the detector so the spike is ignored.
- **Decision:** **reject and fix the data instead.** `reset_lab.py` now scales
  the healthy batch to the same-weekday median from the history itself.
- **Why:** weakening a detector to make bad test data look healthy is how real
  monitoring dies. The dataset was inconsistent; the detector was right.

## Decision 4 — PSI alone is unusable at lab sample sizes

- **Hypothesis:** PSI ≥ 0.25 is the industry-standard drift threshold, so it can
  replace the starter's mean ratio.
- **Evidence:** measured against two samples from the **same** distribution, PSI
  alone flagged **38/40 at n=20** and **27/40 at n=50**. A detector that fires
  95% of the time on healthy data is worse than none.
- **Decision:** **revise.** KS supplies the sample-size-aware significance test
  (`D > c·√((n+m)/(n·m))`), PSI supplies the effect size, and both must agree.
- **Evidence after:** false positives fell to **0–3/60** across n = 20…300,
  while a 15% mean shift, a same-mean bimodal split and a variance blow-up are
  all still caught.

## Decision 5 — Median *ratio* explodes for metrics centred near zero

- **Hypothesis:** a large median ratio is a robust, sample-size-independent
  level-shift signal.
- **Evidence:** for data centred on zero, `median 0.05 / median -0.02` reads as
  a 2.5x "shift" — pure noise. Even after fix 4, false positives stayed ~50%.
- **Decision:** **revise.** Replaced with a standardised shift
  `|Δmedian| / robust_scale(baseline)`; the ratio is only consulted when the
  baseline sits clearly away from zero.
- **Why:** an unstable statistic that happens to work on revenue would have
  silently misfired on every rate, delta and centred metric.

## Decision 6 — A 1000x burn rate from one failed check must not page

- **Hypothesis:** the burn-rate maths from the SRE workbook is enough.
- **Evidence:** the first run after a fault gave burn 1000x on both windows and
  paged — with a two-run history. Arithmetically true, operationally useless.
- **Decision:** **accept with a guard.** Added an optional
  `long_window_events` / `min_long_window_events=5` significance check. Default
  behaviour is unchanged, so the stable API still works with two floats.
- **Evidence after:** the first bad run reports `insufficient data`; once the
  window fills, the sustained burn pages (short 1000x / long 461x → critical).

## Decision 7 — Fix the revenue model, do not just document the bug

- **Hypothesis:** a duplicated *active* customer row inflates revenue through
  join fan-out.
- **Prompt:** "Write the smallest dbt unit test that exposes revenue inflation
  when the customer dimension has two active rows. Do not modify the model yet."
- **Evidence:** the unit test failed exactly as predicted —
  `daily_revenue 100.0 → 200.0`, `completed_order_rows 1 → 2`. On the real
  dataset with 15 broken customers the un-fixed model reports **$9,635.81 against
  a true $8,230.12 (+17.1%)**.
- **Decision:** **accept, then fix.** Deduplicated the dimension with
  `qualify row_number()`, and added `assert_one_active_row_per_customer` so the
  upstream defect is still reported rather than silently absorbed.

## Decision 8 — De-trending compared against the wrong expectation

- **Hypothesis:** de-trending stops a steadily growing metric from alerting daily.
- **Evidence:** a test on 10%-compounding growth still alerted. The
  relative-change guard was measuring against the **median of the raw window**
  (139.5) instead of the value the trend projects for this step (203).
- **Decision:** **accept the fix** — recompute the guard against the projected
  value after de-trending. A genuine bug the test caught, not a tuning issue.

## Decision 9 — Emit OpenLineage to a real server, not just spec-shaped JSON

- **Hypothesis:** hand-built JSON that matches the OpenLineage schema is
  equivalent to using the client.
- **Evidence:** it is not. Running a real Marquez surfaced three things the
  offline path never would have: `marquez-web` needs `WEB_PORT` or it exits 1;
  Marquez stores `DataQualityAssertions` on the **input dataset versions of a
  run**, not on the dataset (my first verification query read the wrong path and
  reported zero assertions that were in fact stored); and one job per lineage
  *edge* produces a technically valid graph that is unusable in the UI, so the
  emitter was reshaped to one job per transformation node.
- **Decision:** **accept both.** `openlineage-python` over HTTP is the primary
  path; the dependency-free emitter stays for `--offline` and for machines
  without Docker.
- **Why:** "it matches the schema" is a claim about a document. "Marquez shows
  the failed run and the column lineage" is a claim about the system, and only
  the second one is worth anything during an incident.

---

## Where the agent was not trusted

* Nothing was accepted on the strength of a plausible explanation: every
  detector change was measured (false-positive counts over 40–200 draws, real
  fault runs, dbt output) before being kept.
* Three proposals were rejected or reworked (Decisions 3, 4, 5) precisely because
  the measurement disagreed with the reasoning.
