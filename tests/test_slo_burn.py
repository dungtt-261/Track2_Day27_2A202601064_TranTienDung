"""Multi-window burn-rate policy: page on sustained burn, stay quiet on blips."""
import pytest

from observability.slo import calculate_slo, evaluate_multiwindow_burn, rolling_burn


def test_lab_guide_worked_example():
    """SLO 99.5%, 2 bad checks out of 100."""
    result = calculate_slo(0.995, bad_events=2, total_events=100)
    assert result["allowed_bad_rate"] == pytest.approx(0.005)
    assert result["actual_bad_rate"] == pytest.approx(0.02)
    assert result["burn_rate"] == pytest.approx(4.0)
    assert result["breached"] is True
    assert result["error_budget_events"] == pytest.approx(0.5)
    assert result["remaining_error_budget_fraction"] == 0.0


def test_sustained_fast_burn_pages():
    result = evaluate_multiwindow_burn(short_window_burn=20.0, long_window_burn=18.0)
    assert result["page"] is True
    assert result["severity"] == "critical"


def test_transient_spike_does_not_page():
    """The short window is on fire, but the long window says the budget impact
    is not significant. Paging here is how teams learn to ignore pages."""
    result = evaluate_multiwindow_burn(short_window_burn=30.0, long_window_burn=0.4)
    assert result["page"] is False
    assert result["alert_class"] == "none"
    assert "transient" in result["reason"]


def test_already_recovered_incident_does_not_page_but_leaves_a_ticket():
    result = evaluate_multiwindow_burn(short_window_burn=0.2, long_window_burn=15.0)
    assert result["page"] is False
    assert result["ticket"] is True


def test_slow_burn_creates_a_ticket_not_a_page():
    result = evaluate_multiwindow_burn(short_window_burn=3.5, long_window_burn=3.2)
    assert (result["page"], result["alert_class"]) == (False, "ticket")


def test_medium_tier_pages():
    result = evaluate_multiwindow_burn(short_window_burn=8.0, long_window_burn=7.0)
    assert result["page"] is True
    assert result["severity"] == "high"


def test_healthy_is_silent():
    result = evaluate_multiwindow_burn(short_window_burn=0.5, long_window_burn=0.4)
    assert (result["page"], result["ticket"]) == (False, False)


def test_single_bad_check_does_not_page_a_tiny_window():
    """1 bad check out of 2 is a 1000x burn rate and means nothing."""
    result = evaluate_multiwindow_burn(
        short_window_burn=500.0, long_window_burn=500.0, long_window_events=2
    )
    assert result["page"] is False
    assert "insufficient data" in result["reason"]


def test_rolling_burn_pages_on_a_sustained_outage():
    outcomes = [False] * 20 + [True] * 10        # oldest first, True == bad check
    result = rolling_burn(0.99, outcomes, short_window=5, long_window=30)
    assert result["decision"]["page"] is True


def test_rolling_burn_stays_quiet_after_recovery():
    outcomes = [False] * 10 + [True] * 5 + [False] * 15
    result = rolling_burn(0.99, outcomes, short_window=5, long_window=30)
    assert result["decision"]["page"] is False


@pytest.mark.parametrize("bad,total", [(-1, 10), (5, 3)])
def test_invalid_counts_raise(bad, total):
    with pytest.raises(ValueError):
        calculate_slo(0.99, bad, total)


def test_zero_events_is_safe():
    result = calculate_slo(0.99, 0, 0)
    assert result["burn_rate"] == 0 and result["breached"] is False
