"""Anomaly cases the starter z-score gets wrong.

Each test states the operational question, not just the maths.
"""
import numpy as np
import pytest

from observability.anomaly import detect_anomaly

WEEKDAY = [600, 610, 595, 608, 612, 590, 605]
SATURDAY = [258, 262, 250, 255, 247, 251]
MIXED = [600, 610, 595, 258, 262, 608, 612, 590, 250, 255, 600, 615, 588, 247]
MIXED_DOW = [0, 1, 2, 5, 6, 0, 1, 2, 5, 6, 0, 1, 2, 5]


def test_true_volume_drop_is_detected():
    assert detect_anomaly(180, WEEKDAY, method="auto")["is_anomaly"] is True


def test_healthy_saturday_is_not_an_anomaly_with_same_segment_history():
    """A quiet weekend is not an incident. The starter compares against a mixed
    window and has to choose between missing real drops and paging every Saturday."""
    result = detect_anomaly(
        252, MIXED, method="auto",
        context={"metric_name": "row_count", "day_of_week": 5, "same_segment_history": SATURDAY},
    )
    assert result["is_anomaly"] is False
    assert "same_segment_history" in result["baseline_source"]


def test_saturday_segment_is_derived_from_parallel_day_of_week_list():
    result = detect_anomaly(
        252, MIXED, method="auto",
        context={"metric_name": "row_count", "day_of_week": 5, "history_day_of_week": MIXED_DOW},
    )
    assert result["is_anomaly"] is False
    assert "day_of_week=5" in result["baseline_source"]


def test_real_drop_on_a_saturday_still_fires():
    """The seasonality guard must not become a blindfold."""
    result = detect_anomaly(
        75, MIXED, method="auto",
        context={"metric_name": "row_count", "day_of_week": 5, "same_segment_history": SATURDAY},
    )
    assert result["is_anomaly"] is True
    assert result["direction"] == "drop"


def test_mixed_history_blinds_the_plain_zscore():
    """Bimodal history inflates std, so z-score misses a 75% collapse entirely."""
    assert detect_anomaly(63, MIXED, method="zscore")["is_anomaly"] is False
    assert detect_anomaly(
        63, MIXED, method="auto", context={"same_segment_history": SATURDAY}
    )["is_anomaly"] is True


def test_contaminated_history_masks_the_zscore_but_not_mad():
    """One past incident in the window inflates std and hides the next one."""
    contaminated = [1000, 1010, 995, 1008, 300, 1004, 1012, 998]
    assert detect_anomaly(400, contaminated, method="zscore")["is_anomaly"] is False
    assert detect_anomaly(400, contaminated, method="auto")["is_anomaly"] is True


def test_zero_mad_constant_history_is_handled():
    """The starter returned `mad_is_zero_todo` and never alerted."""
    result = detect_anomaly(0, [1000] * 8, method="mad")
    assert result["is_anomaly"] is True
    assert result["scale_source"] == "constant_history"


def test_zero_mad_with_a_few_distinct_values_uses_a_fallback_scale():
    result = detect_anomaly(500, [1000, 1000, 1000, 1000, 1000, 1001, 999], method="mad")
    assert result["is_anomaly"] is True
    assert result["scale_source"] == "mean_abs_deviation_fallback"


def test_constant_history_does_not_alert_on_the_same_value():
    assert detect_anomaly(1000, [1000] * 8, method="mad")["is_anomaly"] is False


def test_tiny_deviation_on_a_tight_baseline_does_not_page():
    """3 sigma of nothing is still nothing - alert-fatigue guard."""
    result = detect_anomaly(1002, [1000, 1000, 1000, 1001, 999, 1000, 1000], method="auto")
    assert result["is_anomaly"] is False
    assert result["suppressed"] is True


def test_announced_event_is_suppressed_but_still_scored():
    result = detect_anomaly(
        3000, [600] * 10, method="auto", context={"known_event": "black_friday"}
    )
    assert result["is_anomaly"] is False
    assert result["raw_is_anomaly"] is True


def test_growing_metric_is_detrended_instead_of_alerting_daily():
    trend = [100, 110, 121, 133, 146, 161, 177, 195]
    assert detect_anomaly(214, trend, method="auto")["is_anomaly"] is False
    assert detect_anomaly(60, trend, method="auto")["is_anomaly"] is True


def test_short_history_falls_back_to_relative_change():
    result = detect_anomaly(100, [1000, 1010], method="auto")
    assert result["is_anomaly"] is True
    assert result["method"] == "auto:relative_change"


def test_empty_history_never_alerts():
    assert detect_anomaly(100, [], method="auto")["is_anomaly"] is False


@pytest.mark.parametrize("method", ["zscore", "mad", "ewma", "iqr", "auto"])
def test_every_method_returns_the_stable_shape(method):
    result = detect_anomaly(100, list(np.arange(90, 110)), method=method)
    assert {"is_anomaly", "score", "method", "reason"} <= set(result)
    assert isinstance(result["is_anomaly"], bool)


def test_unknown_method_raises():
    with pytest.raises(ValueError):
        detect_anomaly(1, [1, 2, 3], method="magic")
