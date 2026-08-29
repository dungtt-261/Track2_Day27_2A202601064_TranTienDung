"""Anomaly detection for daily data-quality metrics.

The starter shipped a plain z-score. Z-score breaks in exactly the situations
this lab cares about:

1. **Seasonality** - weekend traffic is ~43% of weekday traffic, so a healthy
   Saturday looks like a 57% "drop" against a mixed 14-day window.
2. **Contaminated history** - the mean/std are computed from the same history
   that contains yesterday's incident, so a real outlier inflates ``std`` and
   masks itself (masking effect).
3. **Trend** - a steadily growing metric drifts away from a stale mean.
4. **Zero variance** - ``std == 0`` (or ``MAD == 0``) makes the score undefined.

``auto`` therefore uses a *robust, seasonality-aware* baseline:
same-segment history when available, median/MAD instead of mean/std, an
explicit fallback chain when MAD collapses to zero, optional de-trending, a
minimum-relative-change guard against alert fatigue, and suppression for
announced events.
"""
from __future__ import annotations

from typing import Any, Iterable, Sequence

import numpy as np

#: 1/Phi^-1(0.75); scales MAD so it estimates sigma for normal data.
MAD_TO_SIGMA = 1.4826
MODIFIED_Z_CONST = 0.6745  # == 1 / MAD_TO_SIGMA

#: Below this relative deviation we never page, no matter how tight the
#: historical variance was. Prevents "3-sigma of nothing" alert fatigue.
DEFAULT_MIN_RELATIVE_CHANGE = 0.10


def _as_array(values: Iterable[float]) -> np.ndarray:
    arr = np.asarray([v for v in values], dtype=float)
    return arr[np.isfinite(arr)]


def _result(
    *,
    is_anomaly: bool,
    score: float,
    method: str,
    reason: str,
    **extra: Any,
) -> dict[str, Any]:
    payload = {
        "is_anomaly": bool(is_anomaly),
        "score": float(score),
        "method": method,
        "reason": reason,
    }
    payload.update(extra)
    return payload


def _direction(current: float, expected: float) -> str:
    if current < expected:
        return "drop"
    if current > expected:
        return "spike"
    return "flat"


def _relative_change(current: float, expected: float) -> float:
    if expected == 0:
        return float("inf") if current != 0 else 0.0
    return abs(current - expected) / abs(expected)


# --------------------------------------------------------------------------- #
# individual detectors
# --------------------------------------------------------------------------- #
def zscore_detector(current: float, history: Iterable[float], threshold: float = 3.0) -> dict[str, Any]:
    """Classic mean/std z-score. Kept unchanged as the teaching baseline."""
    values = _as_array(history)
    if values.size < 3:
        return _result(is_anomaly=False, score=0.0, method="zscore", reason="insufficient_history")
    mean = float(np.mean(values))
    std = float(np.std(values))
    if std == 0:
        score = float("inf") if float(current) != mean else 0.0
    else:
        score = abs(float(current) - mean) / std
    return _result(
        is_anomaly=score > threshold,
        score=score,
        method="zscore",
        reason=f"mean={mean:.3f}, std={std:.3f}, threshold={threshold}",
        expected=mean,
        direction=_direction(float(current), mean),
    )


def mad_detector(
    current: float,
    history: Iterable[float],
    threshold: float = 3.5,
    *,
    min_points: int = 5,
) -> dict[str, Any]:
    """Median-absolute-deviation detector with a real zero-MAD fallback chain.

    When more than half the history is identical, ``MAD == 0`` and the modified
    z-score is undefined. The starter gave up there. We fall back to the mean
    absolute deviation, and finally treat a perfectly constant series as
    "any deviation is an anomaly", which is the operationally correct answer for
    a metric that has never moved.
    """
    values = _as_array(history)
    if values.size < min_points:
        return _result(is_anomaly=False, score=0.0, method="mad", reason="insufficient_history")

    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median)))
    current = float(current)
    scale_source = "mad"

    if mad == 0:
        mean_abs_dev = float(np.mean(np.abs(values - median)))
        if mean_abs_dev > 0:
            # 1.253314 == sqrt(pi/2); scales MeanAD to sigma for normal data.
            sigma = 1.253314 * mean_abs_dev
            score = abs(current - median) / sigma
            scale_source = "mean_abs_deviation_fallback"
            return _result(
                is_anomaly=score > threshold,
                score=score,
                method="mad",
                reason=(
                    f"median={median:.3f}, mad=0 -> {scale_source}, "
                    f"sigma={sigma:.3f}, threshold={threshold}"
                ),
                expected=median,
                direction=_direction(current, median),
                scale_source=scale_source,
            )
        # Perfectly constant history.
        deviates = current != median
        return _result(
            is_anomaly=deviates,
            score=float("inf") if deviates else 0.0,
            method="mad",
            reason=f"constant_history median={median:.3f}; any deviation is anomalous",
            expected=median,
            direction=_direction(current, median),
            scale_source="constant_history",
        )

    modified_z = MODIFIED_Z_CONST * abs(current - median) / mad
    return _result(
        is_anomaly=modified_z > threshold,
        score=modified_z,
        method="mad",
        reason=f"median={median:.3f}, mad={mad:.3f}, threshold={threshold}",
        expected=median,
        direction=_direction(current, median),
        scale_source=scale_source,
    )


def ewma_detector(
    current: float,
    history: Iterable[float],
    *,
    alpha: float = 0.3,
    threshold: float = 3.0,
) -> dict[str, Any]:
    """Exponentially weighted baseline - reacts to level shifts and trend."""
    values = _as_array(history)
    if values.size < 3:
        return _result(is_anomaly=False, score=0.0, method="ewma", reason="insufficient_history")
    level = float(values[0])
    residuals: list[float] = []
    for value in values[1:]:
        residuals.append(value - level)
        level = alpha * value + (1 - alpha) * level
    if not residuals:
        return _result(is_anomaly=False, score=0.0, method="ewma", reason="insufficient_history")
    spread = float(np.median(np.abs(np.asarray(residuals) - np.median(residuals)))) * MAD_TO_SIGMA
    if spread == 0:
        spread = float(np.std(residuals))
    current = float(current)
    if spread == 0:
        deviates = current != level
        return _result(
            is_anomaly=deviates,
            score=float("inf") if deviates else 0.0,
            method="ewma",
            reason=f"ewma_level={level:.3f}, zero residual spread",
            expected=level,
            direction=_direction(current, level),
        )
    score = abs(current - level) / spread
    return _result(
        is_anomaly=score > threshold,
        score=score,
        method="ewma",
        reason=f"ewma_level={level:.3f}, alpha={alpha}, spread={spread:.3f}, threshold={threshold}",
        expected=level,
        direction=_direction(current, level),
    )


def iqr_detector(current: float, history: Iterable[float], *, k: float = 1.5) -> dict[str, Any]:
    """Tukey fences - distribution-free, useful for skewed metrics."""
    values = _as_array(history)
    if values.size < 4:
        return _result(is_anomaly=False, score=0.0, method="iqr", reason="insufficient_history")
    q1, q3 = (float(x) for x in np.percentile(values, [25, 75]))
    iqr = q3 - q1
    lower, upper = q1 - k * iqr, q3 + k * iqr
    current = float(current)
    median = float(np.median(values))
    outside = current < lower or current > upper
    score = 0.0 if iqr == 0 else max(0.0, (lower - current) / iqr, (current - upper) / iqr)
    return _result(
        is_anomaly=bool(outside),
        score=score,
        method="iqr",
        reason=f"q1={q1:.3f}, q3={q3:.3f}, fences=[{lower:.3f}, {upper:.3f}], k={k}",
        expected=median,
        direction=_direction(current, median),
    )


# --------------------------------------------------------------------------- #
# seasonality-aware baseline selection
# --------------------------------------------------------------------------- #
def select_baseline(
    history: Iterable[float], context: dict[str, Any] | None
) -> tuple[np.ndarray, str]:
    """Pick the most specific usable history window.

    Priority: caller-provided same-segment history > history filtered by
    ``day_of_week`` (when a parallel day-of-week list is supplied) > raw history.
    """
    context = context or {}
    raw = _as_array(history)

    for key in ("same_segment_history", "segment_history", "same_weekday_history"):
        segment = _as_array(context.get(key) or [])
        if segment.size >= 3:
            return segment, f"same_segment_history(n={segment.size})"

    dows: Sequence[Any] | None = (
        context.get("history_day_of_week") or context.get("history_days_of_week")
    )
    dow = context.get("day_of_week")
    if dows is not None and dow is not None and len(list(dows)) == raw.size:
        mask = np.asarray([d == dow for d in dows], dtype=bool)
        segment = raw[mask]
        if segment.size >= 3:
            return segment, f"day_of_week={dow} segment(n={segment.size})"

    return raw, f"full_history(n={raw.size})"


def _detrend(values: np.ndarray, current: float) -> tuple[np.ndarray, float, str]:
    """Remove a linear trend so a growing metric is not flagged every day."""
    n = values.size
    if n < 6:
        return values, current, "no_detrend(short_history)"
    x = np.arange(n, dtype=float)
    slope, intercept = np.polyfit(x, values, 1)
    spread = float(np.median(np.abs(values - np.median(values)))) * MAD_TO_SIGMA
    # Only de-trend when the drift across the window is large versus the noise.
    if spread == 0 or abs(slope) * n < 2 * spread:
        return values, current, "no_detrend(trend_within_noise)"
    fitted = slope * x + intercept
    residuals = values - fitted
    projected = slope * n + intercept
    return residuals, float(current) - projected, f"detrended(slope={slope:.3f}/step)"


def auto_detector(
    current: float,
    history: Iterable[float],
    *,
    threshold: float = 3.5,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Context-aware detector used by ``method="auto"``."""
    context = dict(context or {})
    current = float(current)
    metric_name = context.get("metric_name", "metric")
    baseline, baseline_source = select_baseline(history, context)

    if baseline.size == 0:
        return _result(
            is_anomaly=False,
            score=0.0,
            method="auto:none",
            reason="no_history",
            baseline_source=baseline_source,
            metric_name=metric_name,
        )

    expected = float(np.median(baseline))
    rel_change = _relative_change(current, expected)
    min_rel = float(context.get("min_relative_change", DEFAULT_MIN_RELATIVE_CHANGE))

    # Very short history: statistics are meaningless, fall back to a blunt but
    # honest relative-change rule so a catastrophic drop is still visible.
    if baseline.size < 3:
        big = rel_change >= max(0.5, min_rel)
        return _result(
            is_anomaly=bool(big),
            score=float(rel_change),
            method="auto:relative_change",
            reason=(
                f"only {baseline.size} baseline point(s); "
                f"relative_change={rel_change:.1%} vs expected={expected:.3f}"
            ),
            expected=expected,
            direction=_direction(current, expected),
            baseline_source=baseline_source,
            relative_change=rel_change,
            metric_name=metric_name,
        )

    working, working_current, trend_note = _detrend(baseline, current)
    if trend_note.startswith("detrended"):
        # After de-trending, "expected" is the value the trend projects for this
        # step, so the relative-change guard has to be measured against that -
        # not against the median of a window the metric has already grown past.
        expected = float(current - working_current)
        rel_change = _relative_change(current, expected)

    primary = mad_detector(working_current, working, threshold=threshold, min_points=3)
    used = "mad"
    if primary.get("reason") == "insufficient_history":  # pragma: no cover - guarded above
        primary, used = zscore_detector(working_current, working, threshold=threshold), "zscore"

    score = float(primary["score"])
    is_anomaly = bool(primary["is_anomaly"])
    notes = [f"baseline={baseline_source}", trend_note, primary["reason"]]

    # Guard against pathologically tight baselines.
    suppressed_by_guard = False
    if is_anomaly and np.isfinite(rel_change) and rel_change < min_rel:
        is_anomaly = False
        suppressed_by_guard = True
        notes.append(f"suppressed: relative_change={rel_change:.1%} < min_relative_change={min_rel:.0%}")

    # A second, independent opinion for level shifts the robust score can miss
    # when the segment window is short.
    if not is_anomaly and not suppressed_by_guard and baseline.size >= 5:
        ewma = ewma_detector(working_current, working, threshold=threshold)
        if ewma["is_anomaly"] and rel_change >= min_rel:
            is_anomaly, score, used = True, float(ewma["score"]), "mad+ewma"
            notes.append("escalated_by_ewma: " + ewma["reason"])

    known_event = context.get("known_event")
    suppressed_by_event = False
    if is_anomaly and known_event:
        is_anomaly = False
        suppressed_by_event = True
        notes.append(f"suppressed: known_event={known_event}")

    return _result(
        is_anomaly=is_anomaly,
        score=score,
        method=f"auto:{used}",
        reason="; ".join(n for n in notes if n),
        expected=expected,
        direction=_direction(current, expected),
        relative_change=None if not np.isfinite(rel_change) else float(rel_change),
        baseline_source=baseline_source,
        baseline_points=int(baseline.size),
        metric_name=metric_name,
        suppressed=bool(suppressed_by_event or suppressed_by_guard),
        raw_is_anomaly=bool(primary["is_anomaly"]),
    )


DETECTORS = {
    "zscore": zscore_detector,
    "mad": mad_detector,
    "ewma": ewma_detector,
    "iqr": iqr_detector,
}


def detect_anomaly(
    current: float,
    history: Iterable[float],
    *,
    method: str = "auto",
    threshold: float | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Stable lab API.

    ``zscore`` / ``mad`` / ``ewma`` / ``iqr`` run a single detector.
    ``auto`` runs the seasonality-aware robust policy described in the module
    docstring and is the one the on-call pipeline should use.
    """
    if method == "auto":
        return auto_detector(current, history, threshold=threshold or 3.5, context=context)
    if method == "zscore":
        return zscore_detector(current, history, threshold=threshold or 3.0)
    if method == "mad":
        return mad_detector(current, history, threshold=threshold or 3.5)
    if method == "ewma":
        return ewma_detector(current, history, threshold=threshold or 3.0)
    if method == "iqr":
        return iqr_detector(current, history)
    raise ValueError(f"Unsupported method: {method}")
