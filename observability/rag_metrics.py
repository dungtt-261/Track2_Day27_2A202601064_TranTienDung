"""Data-quality signals for the RAG / support-agent branch of the pipeline.

A retrieval pipeline fails quietly: the index still returns *something*, the
agent still answers confidently, and nobody sees a stack trace. The signals
that actually catch it are:

* **content collapse** - chunks suddenly much shorter/longer (bad parse,
  truncated PDF extraction, wrong splitter),
* **embedding drift** - the norm/scale distribution of the vectors moves,
  which is what a swapped or mis-normalised embedding model looks like,
* **staleness** - the newest document is older than the contract allows,
* **version regression** - the index served an *older* revision of a policy
  than it used to. This is the failure in the lab scenario: the agent answers
  with the previous refund policy while every pipeline stage reports SUCCESS.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from observability.anomaly import detect_anomaly, zscore_detector
from observability.distribution import detect_distribution_shift


def approximate_token_lengths(texts: Iterable[str]) -> list[int]:
    # Deliberately simple proxy; no tokenizer/model download needed.
    return [len(str(t).split()) for t in texts]


def detect_text_length_shift(
    current_texts: Iterable[str],
    baseline_batch_means: Iterable[float],
    *,
    threshold: float = 3.0,
) -> dict[str, Any]:
    """Mean chunk length of the current batch vs the history of batch means.

    Uses the robust ``auto`` detector so one bad historical batch cannot inflate
    the spread and mask a real collapse; falls back to the classic z-score
    result for the ``score`` field when the robust baseline is unusable.
    """
    lengths = approximate_token_lengths(current_texts)
    current_mean = float(np.mean(lengths)) if lengths else 0.0
    baseline = list(baseline_batch_means)

    result = detect_anomaly(
        current_mean,
        baseline,
        method="auto",
        threshold=threshold,
        context={"metric_name": "mean_text_length"},
    )
    fallback = zscore_detector(current_mean, baseline, threshold=threshold)
    result["is_anomaly"] = bool(result["is_anomaly"] or fallback["is_anomaly"])
    result["metric"] = "mean_text_length"
    result["current_mean"] = current_mean
    result["zscore"] = fallback["score"]
    result["p10_length"] = float(np.percentile(lengths, 10)) if lengths else 0.0
    result["empty_chunks"] = int(sum(1 for n in lengths if n == 0))
    return result


def detect_embedding_norm_shift(
    current_norms: Iterable[float],
    baseline_norms: Iterable[float],
    *,
    threshold: float = 3.5,
) -> dict[str, Any]:
    """Embedding-space drift from precomputed vector norms.

    Two independent views, because they fail differently:

    * a robust z-score on the *mean* norm catches a global rescale (a swapped
      or newly un-normalised embedding model),
    * PSI/KS on the full norm distribution catches a *subset* of documents
      being embedded differently while the mean barely moves.
    """
    current = np.asarray([float(x) for x in current_norms], dtype=float)
    baseline = np.asarray([float(x) for x in baseline_norms], dtype=float)
    current = current[np.isfinite(current)]
    baseline = baseline[np.isfinite(baseline)]

    if current.size == 0 or baseline.size == 0:
        return {
            "is_anomaly": False,
            "score": 0.0,
            "method": "embedding_norm:empty",
            "reason": "empty_input",
        }

    level = detect_anomaly(
        float(np.mean(current)),
        baseline.tolist(),
        method="auto",
        threshold=threshold,
        context={"metric_name": "embedding_norm_mean"},
    )
    shape = detect_distribution_shift(current.tolist(), baseline.tolist())

    base_std = float(np.std(baseline))
    cur_std = float(np.std(current))
    variance_ratio = (
        float("inf") if base_std == 0 and cur_std > 0 else (cur_std / base_std if base_std else 1.0)
    )
    degenerate = bool(np.any(current <= 0))

    triggers = []
    if level["is_anomaly"]:
        triggers.append(f"mean_norm_shift({level['reason'][:60]})")
    if shape["is_anomaly"]:
        triggers.append(f"distribution_shift({shape['method']}, psi={shape.get('psi', 0):.3f})")
    if variance_ratio > 3 or variance_ratio < 1 / 3:
        triggers.append(f"variance_ratio={variance_ratio:.2f}")
    if degenerate:
        triggers.append("zero_or_negative_norms (degenerate vectors)")

    return {
        "is_anomaly": bool(triggers),
        "score": float(max(level["score"], shape["score"]) if triggers else level["score"]),
        "method": "embedding_norm:robust_z+psi",
        "reason": "; ".join(triggers) if triggers else "embedding norms stable vs baseline",
        "current_mean_norm": float(np.mean(current)),
        "baseline_mean_norm": float(np.mean(baseline)),
        "variance_ratio": variance_ratio,
        "psi": float(shape.get("psi", 0.0)),
        "severity": "critical" if degenerate or len(triggers) > 1 else ("warning" if triggers else "info"),
    }


def detect_version_regression(
    current_docs: Iterable[Mapping[str, Any]],
    baseline_docs: Iterable[Mapping[str, Any]],
    *,
    id_field: str = "doc_id",
    version_field: str = "version",
) -> dict[str, Any]:
    """Catch an index that rolled *back* to older revisions of documents."""
    baseline_versions = {
        d.get(id_field): d.get(version_field) for d in baseline_docs if d.get(id_field) is not None
    }
    regressed, missing = [], []
    for doc in current_docs:
        doc_id = doc.get(id_field)
        if doc_id is None or doc_id not in baseline_versions:
            continue
        try:
            if float(doc.get(version_field)) < float(baseline_versions[doc_id]):
                regressed.append(
                    {
                        "doc_id": doc_id,
                        "current_version": doc.get(version_field),
                        "baseline_version": baseline_versions[doc_id],
                    }
                )
        except (TypeError, ValueError):
            continue
    current_ids = {d.get(id_field) for d in current_docs}
    missing = sorted(str(i) for i in baseline_versions.keys() - current_ids)
    return {
        "is_anomaly": bool(regressed or missing),
        "score": float(len(regressed) + len(missing)),
        "method": "version_regression",
        "reason": (
            f"regressed_docs={regressed}; missing_docs={missing}"
            if regressed or missing
            else "all documents at or above their baseline version"
        ),
        "regressed_docs": regressed,
        "missing_docs": missing,
        "severity": "critical" if regressed else ("warning" if missing else "info"),
    }


def kb_staleness(
    docs: Sequence[Mapping[str, Any]],
    *,
    max_delay_minutes: float = 60,
    timestamp_field: str = "published_at",
    now: Any | None = None,
) -> dict[str, Any]:
    """Freshness SLI for the knowledge base that feeds the RAG index."""
    if not docs:
        return {
            "is_anomaly": True,
            "score": float("inf"),
            "method": "kb_staleness",
            "reason": "knowledge base is empty",
            "age_minutes": None,
            "stale_documents": [],
            "severity": "critical",
        }
    reference = pd.Timestamp(now or datetime.now(timezone.utc))
    if reference.tzinfo is None:
        reference = reference.tz_localize("UTC")
    parsed = pd.to_datetime(
        [d.get(timestamp_field) for d in docs], utc=True, errors="coerce", format="mixed"
    )
    ages = [(reference - ts).total_seconds() / 60.0 if pd.notna(ts) else None for ts in parsed]
    stale = [
        {"doc_id": docs[i].get("doc_id"), "age_minutes": round(age, 1)}
        for i, age in enumerate(ages)
        if age is None or age > max_delay_minutes
    ]
    valid_ages = [a for a in ages if a is not None]
    newest = min(valid_ages) if valid_ages else None
    return {
        "is_anomaly": bool(stale),
        "score": float(newest) if newest is not None else float("inf"),
        "method": "kb_staleness",
        "reason": (
            f"{len(stale)}/{len(docs)} documents older than {max_delay_minutes:.0f} min; "
            f"newest_document_age={newest:.1f} min"
            if stale and newest is not None
            else f"newest_document_age={newest:.1f} min <= {max_delay_minutes:.0f} min"
            if newest is not None
            else "no parseable publish timestamps"
        ),
        "age_minutes": newest,
        "stale_documents": stale,
        "severity": "critical" if stale else "info",
    }


def retrieval_metrics(
    retrieved: Sequence[Sequence[str]], relevant: Sequence[Sequence[str]], *, k: int = 3
) -> dict[str, Any]:
    """Offline retrieval quality: recall@k, precision@k and MRR."""
    if not retrieved or len(retrieved) != len(relevant):
        return {"recall_at_k": 0.0, "precision_at_k": 0.0, "mrr": 0.0, "queries": 0, "k": k}
    recalls, precisions, rrs = [], [], []
    for got, want in zip(retrieved, relevant):
        top = list(got)[:k]
        want_set = set(want)
        hits = len(want_set & set(top))
        recalls.append(hits / len(want_set) if want_set else 0.0)
        precisions.append(hits / k if k else 0.0)
        rr = 0.0
        for rank, doc in enumerate(top, start=1):
            if doc in want_set:
                rr = 1.0 / rank
                break
        rrs.append(rr)
    return {
        "recall_at_k": float(np.mean(recalls)),
        "precision_at_k": float(np.mean(precisions)),
        "mrr": float(np.mean(rrs)),
        "queries": len(retrieved),
        "k": k,
    }
