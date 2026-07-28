"""Scoring (M0.5).

Architecture.md §8 names diagnoser accuracy *and calibration* as the core quality
metric, and Implementation-Plan.md Phase 0 makes calibration — not accuracy — the
gate for letting generated output reach a student. That ordering is deliberate:
every downstream confidence threshold in §3.1 is meaningless if a 0.9 does not
mean 90%. So calibration is a first-class metric here, not a diagnostic extra.

`unknown` is scored as its own outcome rather than as a wrong answer. §3.1 treats
it as a *correct* behaviour under ambiguity — a confident wrong tag is worse than
none — so folding it into accuracy would penalise the system for doing the right
thing.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from eval.harness.dataset import Example
from packages.domain.enums import UNKNOWN_TAG_LABEL


class Prediction(BaseModel):
    model_config = ConfigDict(frozen=True)

    example_id: str
    label: str
    confidence: float = Field(ge=0.0, le=1.0)


class LabelScore(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: str
    support: int
    precision: float
    recall: float
    f1: float


class CalibrationBucket(BaseModel):
    model_config = ConfigDict(frozen=True)

    lower: float
    upper: float
    count: int
    mean_confidence: float
    accuracy: float

    @property
    def gap(self) -> float:
        """How far the stated confidence is from observed accuracy."""
        return abs(self.mean_confidence - self.accuracy)


class Scores(BaseModel):
    """Everything a suite reports. Flat floats so the regression gate can compare
    any metric by name without knowing what it means."""

    model_config = ConfigDict(frozen=True)

    total: int
    answered: int
    correct: int
    accuracy: float
    accuracy_when_answered: float
    unknown_rate: float
    macro_f1: float
    high_confidence_precision: float
    high_confidence_count: int
    expected_calibration_error: float
    per_label: tuple[LabelScore, ...]
    calibration: tuple[CalibrationBucket, ...]

    def as_metrics(self) -> dict[str, float]:
        """The subset the regression gate tracks."""
        return {
            "accuracy": self.accuracy,
            "accuracy_when_answered": self.accuracy_when_answered,
            "unknown_rate": self.unknown_rate,
            "macro_f1": self.macro_f1,
            "high_confidence_precision": self.high_confidence_precision,
            "expected_calibration_error": self.expected_calibration_error,
        }


HIGH_CONFIDENCE_THRESHOLD = 0.8
"""§8's calibration gate: predictions at or above this must be >=90% correct."""


def _safe_div(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def _per_label(pairs: Sequence[tuple[str, str]]) -> tuple[LabelScore, ...]:
    """pairs are (expected, predicted). Excludes `unknown` — it is an abstention,
    not a class the diagnoser is trying to predict."""
    labels = sorted({e for e, _ in pairs} | {p for _, p in pairs})
    scores: list[LabelScore] = []
    for label in labels:
        if label == UNKNOWN_TAG_LABEL:
            continue
        tp = sum(1 for e, p in pairs if e == label and p == label)
        fp = sum(1 for e, p in pairs if e != label and p == label)
        fn = sum(1 for e, p in pairs if e == label and p != label)
        precision = _safe_div(tp, tp + fp)
        recall = _safe_div(tp, tp + fn)
        scores.append(
            LabelScore(
                label=label,
                support=tp + fn,
                precision=precision,
                recall=recall,
                f1=_safe_div(2 * precision * recall, precision + recall),
            )
        )
    return tuple(scores)


def _calibration(
    records: Sequence[tuple[float, bool]], buckets: int = 5
) -> tuple[tuple[CalibrationBucket, ...], float]:
    """Reliability buckets plus expected calibration error.

    ECE is the support-weighted mean gap between stated confidence and observed
    accuracy — one number for "can these confidences be trusted as probabilities".
    """
    if not records:
        return (), 0.0

    width = 1.0 / buckets
    out: list[CalibrationBucket] = []
    weighted_gap = 0.0
    for index in range(buckets):
        lower = index * width
        upper = (index + 1) * width
        # Top bucket is closed so a confidence of exactly 1.0 lands somewhere.
        in_bucket = [
            (c, ok)
            for c, ok in records
            if (lower <= c < upper) or (index == buckets - 1 and c == 1.0)
        ]
        if not in_bucket:
            continue
        mean_conf = sum(c for c, _ in in_bucket) / len(in_bucket)
        accuracy = sum(1 for _, ok in in_bucket if ok) / len(in_bucket)
        bucket = CalibrationBucket(
            lower=lower,
            upper=upper,
            count=len(in_bucket),
            mean_confidence=mean_conf,
            accuracy=accuracy,
        )
        out.append(bucket)
        weighted_gap += bucket.gap * len(in_bucket)

    return tuple(out), weighted_gap / len(records)


class ScoringError(ValueError):
    """Raised when predictions and examples do not line up."""


def score(
    examples: Sequence[Example],
    predictions: Sequence[Prediction],
    high_confidence_threshold: float = HIGH_CONFIDENCE_THRESHOLD,
) -> Scores:
    """Score one suite run.

    Requires exactly one prediction per example. A missing prediction would
    otherwise silently shrink the denominator and inflate every rate — the
    failure mode where a crashed stage looks like a quality improvement.
    """
    by_id = {p.example_id: p for p in predictions}
    missing = [e.id for e in examples if e.id not in by_id]
    if missing:
        raise ScoringError(f"no prediction for {len(missing)} example(s): {missing[:5]}")
    extra = set(by_id) - {e.id for e in examples}
    if extra:
        raise ScoringError(f"predictions for unknown example(s): {sorted(extra)[:5]}")

    pairs = [(e.expected, by_id[e.id].label) for e in examples]
    answered = [(e, p) for e, p in pairs if p != UNKNOWN_TAG_LABEL]
    correct = sum(1 for e, p in answered if e == p)

    # Calibration is measured on answered predictions only: an abstention has no
    # claim to be right, so scoring its confidence would be meaningless.
    records = [
        (by_id[e.id].confidence, e.expected == by_id[e.id].label)
        for e in examples
        if by_id[e.id].label != UNKNOWN_TAG_LABEL
    ]
    buckets, ece = _calibration(records)

    high = [(c, ok) for c, ok in records if c >= high_confidence_threshold]
    label_scores = _per_label(pairs)

    return Scores(
        total=len(examples),
        answered=len(answered),
        correct=correct,
        accuracy=_safe_div(correct, len(examples)),
        accuracy_when_answered=_safe_div(correct, len(answered)),
        unknown_rate=_safe_div(len(examples) - len(answered), len(examples)),
        macro_f1=_safe_div(sum(s.f1 for s in label_scores), len(label_scores)),
        high_confidence_precision=_safe_div(sum(1 for _, ok in high if ok), len(high)),
        high_confidence_count=len(high),
        expected_calibration_error=ece,
        per_label=label_scores,
        calibration=buckets,
    )
