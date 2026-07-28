"""Suite definition and execution (M0.5).

A `Suite` binds a dataset to a predictor and the metric specs that judge it. The
predictor is injected, so the same suite runs against a live model stage, a
deterministic stub, or a recorded fixture — which is what lets the harness itself
be unit-tested without a network or a key.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from eval.harness.baseline import Baseline, MetricSpec
from eval.harness.dataset import Dataset, Example, Split
from eval.harness.metrics import Prediction, Scores, score

Predictor = Callable[[Example], Prediction]


@dataclass(frozen=True)
class Suite:
    name: str
    dataset: Dataset
    specs: tuple[MetricSpec, ...]
    split: Split = Split.HOLDOUT
    holdout_pct: int = 30

    def examples(self) -> tuple[Example, ...]:
        return self.dataset.subset(self.split, self.holdout_pct)


class SuiteRun(BaseModel):
    """One execution. Carries the dataset hash so the gate can verify
    comparability, and the prompt/model versions so §8 can segment by them."""

    model_config = ConfigDict(frozen=True)

    suite: str
    dataset_name: str
    dataset_version: str
    dataset_hash: str
    split: Split
    prompt_version: str
    model_id: str
    scores: Scores
    predictions: tuple[Prediction, ...]
    ran_at: dt.datetime

    def to_baseline(self) -> Baseline:
        return Baseline(
            suite=self.suite,
            dataset_name=self.dataset_name,
            dataset_version=self.dataset_version,
            dataset_hash=self.dataset_hash,
            prompt_version=self.prompt_version,
            model_id=self.model_id,
            metrics=self.scores.as_metrics(),
            recorded_at=self.ran_at,
        )


def run(
    suite: Suite,
    predictor: Predictor,
    *,
    prompt_version: str,
    model_id: str,
    examples: Sequence[Example] | None = None,
) -> SuiteRun:
    """Run every example through the predictor and score the result."""
    cases = tuple(examples) if examples is not None else suite.examples()
    if not cases:
        raise ValueError(
            f"suite {suite.name!r} selected 0 examples from split {suite.split.value!r} — "
            f"a suite that scores nothing reports a perfect run"
        )

    predictions = tuple(predictor(example) for example in cases)
    return SuiteRun(
        suite=suite.name,
        dataset_name=suite.dataset.name,
        dataset_version=suite.dataset.version,
        dataset_hash=suite.dataset.content_hash,
        split=suite.split,
        prompt_version=prompt_version,
        model_id=model_id,
        scores=score(cases, predictions),
        predictions=predictions,
        ran_at=dt.datetime.now(dt.UTC),
    )
