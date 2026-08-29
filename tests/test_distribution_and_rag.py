"""Distribution drift and RAG-specific signals."""
import numpy as np
import pytest

from observability.distribution import detect_distribution_shift
from observability.rag_metrics import (
    detect_embedding_norm_shift,
    detect_text_length_shift,
    detect_version_regression,
    kb_staleness,
    retrieval_metrics,
)

def rng(seed: int = 27) -> np.random.Generator:
    """A fresh generator per test - a shared one makes results depend on test
    execution order, which is how a "stable input" assertion becomes flaky."""
    return np.random.default_rng(seed)


BASELINE = list(rng(1).normal(70, 10, 500))


def test_identical_distributions_are_not_flagged():
    assert detect_distribution_shift(list(rng(2).normal(70, 10, 200)), BASELINE)["is_anomaly"] is False


def test_gross_level_shift_is_flagged():
    assert detect_distribution_shift(list(rng(3).normal(210, 10, 200)), BASELINE)["is_anomaly"] is True


def test_shape_change_with_an_unchanged_mean_is_flagged():
    """The starter mean-ratio detector scores this 1.01 and says everything is fine."""
    r = rng(4)
    bimodal = list(np.concatenate([r.normal(40, 3, 100), r.normal(100, 3, 100)]))
    assert detect_distribution_shift(bimodal, BASELINE, method="mean_ratio")["is_anomaly"] is False
    assert detect_distribution_shift(bimodal, BASELINE)["is_anomaly"] is True


def test_metric_centred_on_zero_does_not_false_alarm():
    """A median *ratio* explodes near zero; the robust sigma shift does not."""
    flags = 0
    for seed in range(40):
        r = np.random.default_rng(100 + seed)
        flags += detect_distribution_shift(
            list(r.normal(0, 1, 50)), list(r.normal(0, 1, 200))
        )["is_anomaly"]
    assert flags <= 4          # around the nominal 5% alpha, not the 95% PSI alone gave


def test_new_category_is_flagged():
    result = detect_distribution_shift(["USD"] * 60 + ["VND"] * 40, ["USD"] * 400)
    assert result["is_anomaly"] is True
    assert result["unseen_categories"] == ["VND"]


def test_stable_category_mix_is_not_flagged():
    assert detect_distribution_shift(["USD"] * 60, ["USD"] * 400)["is_anomaly"] is False


def test_empty_input_is_safe():
    assert detect_distribution_shift([], BASELINE)["is_anomaly"] is False


# ----------------------------------------------------------------- RAG signals
def test_chunk_length_collapse_is_detected():
    assert detect_text_length_shift(["x y", "a b"], [40, 42, 39, 41, 43, 40, 42])["is_anomaly"]


def test_stable_chunk_length_is_not_flagged():
    assert detect_text_length_shift(["w " * 41] * 5, [40, 42, 39, 41, 43, 40, 42])["is_anomaly"] is False


def test_embedding_norm_rescale_is_detected():
    """The starter returned `not_implemented` and never fired."""
    r = rng(5)
    baseline = list(r.normal(1.0, 0.02, 300))
    result = detect_embedding_norm_shift(list(r.normal(1.4, 0.02, 80)), baseline)
    assert result["is_anomaly"] is True


def test_degenerate_zero_vectors_are_critical():
    r = rng(6)
    baseline = list(r.normal(1.0, 0.02, 300))
    result = detect_embedding_norm_shift(list(r.normal(1.0, 0.02, 70)) + [0.0] * 10, baseline)
    assert result["is_anomaly"] is True
    assert result["severity"] == "critical"


def test_stable_embeddings_rarely_fire():
    """False-positive budget, measured over many draws rather than one lucky
    sample: the KS test runs at alpha=0.05, so a few hits are expected and a
    single-draw assertion would just be flaky."""
    flags = 0
    for seed in range(60):
        r = np.random.default_rng(200 + seed)
        baseline = list(r.normal(1.0, 0.02, 300))
        flags += detect_embedding_norm_shift(list(r.normal(1.0, 0.02, 80)), baseline)["is_anomaly"]
    assert flags <= 6


def test_policy_rollback_is_detected():
    """The exact failure in the lab scenario: the agent answers with refund
    policy v3 while the published policy is v4. No pipeline stage errors."""
    result = detect_version_regression(
        [{"doc_id": "refund-policy", "version": 3}],
        [{"doc_id": "refund-policy", "version": 4}],
    )
    assert result["is_anomaly"] is True
    assert result["severity"] == "critical"


def test_document_disappearing_from_the_index_is_detected():
    result = detect_version_regression(
        [{"doc_id": "a", "version": 1}],
        [{"doc_id": "a", "version": 1}, {"doc_id": "refund-policy", "version": 4}],
    )
    assert result["missing_docs"] == ["refund-policy"]


def test_kb_staleness_uses_the_newest_document():
    docs = [
        {"doc_id": "old", "published_at": "2026-08-29T00:00:00Z"},
        {"doc_id": "new", "published_at": "2026-08-29T12:30:00Z"},
    ]
    result = kb_staleness(docs, max_delay_minutes=60, now="2026-08-29T12:45:00Z")
    assert result["is_anomaly"] is True                 # the old doc is stale
    assert result["age_minutes"] == pytest.approx(15.0)  # newest is 15 min old


def test_empty_kb_is_critical():
    assert kb_staleness([])["severity"] == "critical"


def test_retrieval_metrics():
    result = retrieval_metrics([["a", "b", "c"], ["x", "y", "z"]], [["a"], ["z"]], k=3)
    assert result["recall_at_k"] == pytest.approx(1.0)
    assert result["mrr"] == pytest.approx((1.0 + 1 / 3) / 2)
