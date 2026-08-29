"""Distribution drift detection.

The starter compared means only. A mean ratio is blind to the failures that
actually matter in a data platform:

* a **shape** change with a stable mean (bimodal split, variance blow-up),
* a **categorical** mix shift (currency suddenly 40% VND),
* a **tail** shift (p99 amount doubles while the mean barely moves).

This module therefore combines three complementary signals:

* **PSI** (Population Stability Index) - the industry-standard drift score;
  ``>= 0.10`` is "investigate", ``>= 0.25`` is "significant shift".
* **Two-sample Kolmogorov-Smirnov** statistic with the standard
  ``D_crit = c(alpha) * sqrt((n+m)/(n*m))`` critical value - no SciPy needed.
* **Robust central/scale ratios** (median and IQR) which stay meaningful on the
  small samples this lab works with.
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Any, Iterable, Sequence

import numpy as np

PSI_INVESTIGATE = 0.10
PSI_SIGNIFICANT = 0.25
KS_ALPHA_C = {0.10: 1.22, 0.05: 1.36, 0.01: 1.63}


def _numeric(values: Iterable[Any]) -> tuple[np.ndarray, bool]:
    raw = list(values)
    try:
        arr = np.asarray(raw, dtype=float)
    except (TypeError, ValueError):
        return np.asarray([]), False
    arr = arr[np.isfinite(arr)]
    return arr, True


def population_stability_index(
    current: Sequence[float], baseline: Sequence[float], *, bins: int = 10
) -> float:
    """PSI with baseline quantile bins and Laplace smoothing for empty buckets."""
    base = np.asarray(baseline, dtype=float)
    cur = np.asarray(current, dtype=float)
    if base.size == 0 or cur.size == 0:
        return 0.0
    # At least ~5 observations per bin, otherwise PSI is dominated by sampling
    # noise (an empty bin contributes a large term through log(p_cur/p_base)).
    bins = int(np.clip(min(base.size, cur.size) // 5, 2, bins))
    quantiles = np.unique(np.percentile(base, np.linspace(0, 100, bins + 1)))
    if quantiles.size < 2:
        # Constant baseline: PSI is only meaningful as "did anything move".
        return 0.0 if np.allclose(cur, base[0]) else float("inf")
    edges = np.concatenate(([-np.inf], quantiles[1:-1], [np.inf]))
    base_counts = np.histogram(base, bins=edges)[0].astype(float)
    cur_counts = np.histogram(cur, bins=edges)[0].astype(float)
    eps = 1e-6
    base_pct = (base_counts + eps) / (base_counts.sum() + eps * base_counts.size)
    cur_pct = (cur_counts + eps) / (cur_counts.sum() + eps * cur_counts.size)
    return float(np.sum((cur_pct - base_pct) * np.log(cur_pct / base_pct)))


def categorical_psi(current: Iterable[Any], baseline: Iterable[Any]) -> float:
    cur_counter, base_counter = Counter(current), Counter(baseline)
    categories = set(cur_counter) | set(base_counter)
    if not categories:
        return 0.0
    cur_total = sum(cur_counter.values()) or 1
    base_total = sum(base_counter.values()) or 1
    eps = 1e-6
    psi = 0.0
    for cat in categories:
        c = (cur_counter.get(cat, 0) + eps) / cur_total
        b = (base_counter.get(cat, 0) + eps) / base_total
        psi += (c - b) * math.log(c / b)
    return float(psi)


def ks_statistic(current: Sequence[float], baseline: Sequence[float]) -> float:
    """Two-sample KS statistic D = max |F_current(x) - F_baseline(x)|."""
    cur = np.sort(np.asarray(current, dtype=float))
    base = np.sort(np.asarray(baseline, dtype=float))
    if cur.size == 0 or base.size == 0:
        return 0.0
    grid = np.concatenate([cur, base])
    cdf_cur = np.searchsorted(cur, grid, side="right") / cur.size
    cdf_base = np.searchsorted(base, grid, side="right") / base.size
    return float(np.max(np.abs(cdf_cur - cdf_base)))


def ks_critical_value(n: int, m: int, alpha: float = 0.05) -> float:
    if n == 0 or m == 0:
        return float("inf")
    return KS_ALPHA_C.get(alpha, 1.36) * math.sqrt((n + m) / (n * m))


def _robust_scale(values: np.ndarray) -> float:
    """MAD-based sigma estimate, falling back to IQR and finally to std."""
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median)))
    if mad > 0:
        return 1.4826 * mad
    iqr = float(np.subtract(*np.percentile(values, [75, 25])))
    if iqr > 0:
        return iqr / 1.349
    return float(np.std(values))


def _ratio(current: float, baseline: float) -> float:
    if baseline == 0:
        return float("inf") if current != 0 else 1.0
    if current == 0:
        return float("inf")
    return max(abs(current / baseline), abs(baseline / current))


def detect_distribution_shift(
    current_values: Iterable[Any],
    baseline_values: Iterable[Any],
    *,
    method: str = "auto",
    ratio_threshold: float = 3.0,
    psi_threshold: float = PSI_SIGNIFICANT,
    alpha: float = 0.05,
    level_shift_sigma_threshold: float = 4.0,
) -> dict[str, Any]:
    """Detect that ``current_values`` no longer look like ``baseline_values``."""
    cur_raw, base_raw = list(current_values), list(baseline_values)
    if not cur_raw or not base_raw:
        return {
            "is_anomaly": False,
            "score": 0.0,
            "method": "auto:empty",
            "reason": "empty_input",
        }

    cur, cur_numeric = _numeric(cur_raw)
    base, base_numeric = _numeric(base_raw)
    numeric = cur_numeric and base_numeric and cur.size > 0 and base.size > 0

    if not numeric:
        psi = categorical_psi(cur_raw, base_raw)
        cur_mix = {k: round(v / len(cur_raw), 3) for k, v in Counter(cur_raw).most_common(5)}
        base_mix = {k: round(v / len(base_raw), 3) for k, v in Counter(base_raw).most_common(5)}
        unseen = sorted(set(map(str, cur_raw)) - set(map(str, base_raw)))
        unseen_share = sum(1 for v in cur_raw if str(v) in set(unseen)) / len(cur_raw)
        flagged = (psi >= PSI_SIGNIFICANT and len(cur_raw) >= 20) or unseen_share >= 0.01
        return {
            "is_anomaly": bool(flagged),
            "score": float(psi),
            "method": "auto:categorical_psi",
            "reason": f"categorical_psi={psi:.3f}; current_mix={cur_mix}; baseline_mix={base_mix}",
            "psi": float(psi),
            "unseen_categories": unseen,
            "unseen_share": float(unseen_share),
        }

    if method == "mean_ratio":
        score = _ratio(float(np.mean(cur)), float(np.mean(base)))
        return {
            "is_anomaly": bool(score >= ratio_threshold),
            "score": float(score),
            "method": "mean_ratio",
            "reason": f"baseline_mean={np.mean(base):.3f}, current_mean={np.mean(cur):.3f}",
        }

    psi = population_stability_index(cur, base)
    ks_d = ks_statistic(cur, base)
    ks_crit = ks_critical_value(cur.size, base.size, alpha)

    base_median, cur_median = float(np.median(base)), float(np.median(cur))
    base_scale = _robust_scale(base)
    # Standardised level shift. A plain median *ratio* explodes for metrics
    # centred near zero (0.05 / -0.02 looks like a 2.5x "shift"), so the ratio is
    # only trusted when the baseline sits clearly away from zero.
    if base_scale > 0:
        level_shift_sigma = abs(cur_median - base_median) / base_scale
    else:
        level_shift_sigma = 0.0 if cur_median == base_median else float("inf")
    median_ratio = _ratio(cur_median, base_median)
    ratio_is_meaningful = abs(base_median) > max(base_scale, 1e-12)

    base_iqr = float(np.subtract(*np.percentile(base, [75, 25])))
    cur_iqr = float(np.subtract(*np.percentile(cur, [75, 25])))
    spread_ratio = _ratio(cur_iqr, base_iqr) if base_iqr or cur_iqr else 1.0

    # PSI alone is far too noisy on small samples (empirically ~95% false
    # positives at n=20 for two samples from the *same* distribution). KS
    # supplies the sample-size-aware significance test, PSI supplies the effect
    # size, and a gross robust level shift is trusted on its own because it does
    # not depend on sample size.
    ks_significant = ks_d > ks_crit
    triggers = []
    if ks_significant and psi >= psi_threshold:
        triggers.append(
            f"psi={psi:.3f}>={psi_threshold} and ks_d={ks_d:.3f}>crit={ks_crit:.3f}(alpha={alpha})"
        )
    if level_shift_sigma >= level_shift_sigma_threshold:
        triggers.append(
            f"median shifted {level_shift_sigma:.1f} robust sigma "
            f"({base_median:.3f} -> {cur_median:.3f})"
        )
    elif ratio_is_meaningful and median_ratio >= ratio_threshold:
        triggers.append(f"median_ratio={median_ratio:.2f}>={ratio_threshold}")
    if ks_significant and math.isfinite(spread_ratio) and spread_ratio >= ratio_threshold * 2:
        triggers.append(f"iqr_ratio={spread_ratio:.2f} (variance shift)")

    score = psi if math.isfinite(psi) else float("inf")
    return {
        "is_anomaly": bool(triggers),
        "score": float(score),
        "method": "auto:psi+ks",
        "reason": (
            "; ".join(triggers)
            if triggers
            else f"stable: psi={psi:.3f}, ks_d={ks_d:.3f} vs crit={ks_crit:.3f}, "
            f"level_shift={level_shift_sigma:.2f} sigma"
        ),
        "psi": float(psi),
        "ks_statistic": float(ks_d),
        "ks_critical_value": float(ks_crit),
        "median_ratio": float(median_ratio),
        "level_shift_sigma": float(level_shift_sigma),
        "baseline_median": float(np.median(base)),
        "current_median": float(np.median(cur)),
        "severity": (
            "critical" if psi >= PSI_SIGNIFICANT else "warning" if psi >= PSI_INVESTIGATE else "info"
        ),
    }
