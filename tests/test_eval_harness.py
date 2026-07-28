"""M0.5 — the harness itself is deterministic, so it is unit-tested.

The suites it runs are not (they call models); those are a CLI, tracked for
regression rather than asserted here. But every property the gate's credibility
rests on — stable splits, dataset-attributed metrics, refusing an incomparable
comparison — is pure logic and belongs in CI.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from eval.harness.baseline import (
    Baseline,
    DatasetMismatchError,
    Direction,
    MetricSpec,
    Verdict,
    check,
)
from eval.harness.dataset import Dataset, DatasetError, Example, Split, load_jsonl
from eval.harness.metrics import Prediction, ScoringError, score
from eval.harness.runner import Suite, run
from eval.suites import diagnosis

NOW = dt.datetime(2026, 7, 27, tzinfo=dt.UTC)


def _example(id_: str, expected: str) -> Example:
    return Example(id=id_, inputs={"problem": "What is 7 + 5?"}, expected=expected)


def _dataset(*labels: tuple[str, str]) -> Dataset:
    return Dataset(name="t", version="v1", examples=tuple(_example(i, e) for i, e in labels))


class TestDatasetVersioning:
    def test_hash_ignores_ordering_and_formatting(self) -> None:
        """Reformatting the file must not look like a data change."""
        a = _dataset(("1", "x"), ("2", "y"))
        b = _dataset(("2", "y"), ("1", "x"))
        assert a.content_hash == b.content_hash

    def test_hash_changes_when_a_label_changes(self) -> None:
        a = _dataset(("1", "x"))
        b = _dataset(("1", "z"))
        assert a.content_hash != b.content_hash

    def test_splits_are_stable_when_examples_are_appended(self) -> None:
        """The property a seeded shuffle would not give.

        As teachers label more sessions the dataset grows; if appending moved
        existing examples between splits, holdout cases would leak into the
        few-shot prompts built from train.
        """
        small = _dataset(*[(str(i), "x") for i in range(20)])
        grown = _dataset(*[(str(i), "x") for i in range(40)])

        before = {e.id: small.split(e) for e in small.examples}
        after = {e.id: grown.split(e) for e in grown.examples if e.id in before}
        assert before == after

    def test_split_is_deterministic_across_runs(self) -> None:
        data = _dataset(*[(str(i), "x") for i in range(50)])
        assert [data.split(e) for e in data.examples] == [data.split(e) for e in data.examples]

    def test_train_and_holdout_partition_the_dataset(self) -> None:
        data = _dataset(*[(str(i), "x") for i in range(50)])
        train = {e.id for e in data.subset(Split.TRAIN)}
        holdout = {e.id for e in data.subset(Split.HOLDOUT)}

        assert train | holdout == {e.id for e in data.examples}
        assert not (train & holdout)

    def test_duplicate_ids_are_rejected(self, tmp_path: Path) -> None:
        """A duplicate would double-count and silently weight one case above the rest."""
        path = tmp_path / "d.jsonl"
        path.write_text(
            '{"name": "d", "version": "v1"}\n'
            '{"id": "a", "inputs": {}, "expected": "x"}\n'
            '{"id": "a", "inputs": {}, "expected": "y"}\n',
            encoding="utf-8",
        )
        with pytest.raises(DatasetError, match="duplicate"):
            load_jsonl(path)

    def test_header_is_required(self, tmp_path: Path) -> None:
        path = tmp_path / "d.jsonl"
        path.write_text('{"id": "a", "inputs": {}, "expected": "x"}\n', encoding="utf-8")
        with pytest.raises(DatasetError, match="header"):
            load_jsonl(path)


class TestScoring:
    def test_unknown_is_an_abstention_not_a_wrong_answer(self) -> None:
        """§3.1 treats abstention under ambiguity as correct behaviour, so it
        must not be scored as an error."""
        examples = [_example("1", "a"), _example("2", "b")]
        predictions = [
            Prediction(example_id="1", label="a", confidence=0.9),
            Prediction(example_id="2", label="unknown", confidence=0.0),
        ]
        result = score(examples, predictions)

        assert result.accuracy == 0.5  # 1 of 2 examples
        assert result.accuracy_when_answered == 1.0  # 1 of 1 answered
        assert result.unknown_rate == 0.5

    def test_missing_prediction_is_an_error(self) -> None:
        """Otherwise a crashed stage shrinks the denominator and looks like a win."""
        with pytest.raises(ScoringError, match="no prediction"):
            score(
                [_example("1", "a"), _example("2", "b")],
                [Prediction(example_id="1", label="a", confidence=0.9)],
            )

    def test_stray_prediction_is_an_error(self) -> None:
        with pytest.raises(ScoringError, match="unknown example"):
            score(
                [_example("1", "a")],
                [
                    Prediction(example_id="1", label="a", confidence=0.9),
                    Prediction(example_id="9", label="a", confidence=0.9),
                ],
            )

    def test_high_confidence_precision_measures_only_confident_predictions(self) -> None:
        """§8's gate: predictions at >=0.8 must be right >=90% of the time."""
        examples = [_example(str(i), "a") for i in range(4)]
        predictions = [
            Prediction(example_id="0", label="a", confidence=0.95),  # confident, right
            Prediction(example_id="1", label="b", confidence=0.85),  # confident, wrong
            Prediction(example_id="2", label="b", confidence=0.30),  # unsure, wrong
            Prediction(example_id="3", label="b", confidence=0.20),  # unsure, wrong
        ]
        result = score(examples, predictions)

        assert result.high_confidence_count == 2
        assert result.high_confidence_precision == 0.5  # unsure misses excluded

    def test_overconfidence_shows_up_as_calibration_error(self) -> None:
        """A model that says 0.95 and is right half the time must not look calibrated."""
        examples = [_example(str(i), "a") for i in range(10)]
        predictions = [
            Prediction(example_id=str(i), label="a" if i < 5 else "b", confidence=0.95)
            for i in range(10)
        ]
        result = score(examples, predictions)

        assert result.expected_calibration_error == pytest.approx(0.45)

    def test_well_calibrated_predictions_score_near_zero(self) -> None:
        examples = [_example(str(i), "a") for i in range(10)]
        predictions = [
            Prediction(example_id=str(i), label="a" if i < 9 else "b", confidence=0.9)
            for i in range(10)
        ]
        result = score(examples, predictions)

        assert result.expected_calibration_error == pytest.approx(0.0, abs=1e-9)


class TestRegressionGate:
    SPECS = (
        MetricSpec(name="accuracy", direction=Direction.HIGHER_IS_BETTER, tolerance=0.02),
        MetricSpec(
            name="unknown_rate", direction=Direction.LOWER_IS_BETTER, tolerance=0.02, maximum=0.5
        ),
    )

    def _baseline(self, **metrics: float) -> Baseline:
        return Baseline(
            suite="s",
            dataset_name="t",
            dataset_version="v1",
            dataset_hash="abc123",
            prompt_version="v1",
            model_id="m",
            metrics=metrics,
            recorded_at=NOW,
        )

    def test_dataset_change_refuses_comparison(self) -> None:
        """The gate's most important behaviour.

        Comparing across dataset versions turns a data edit into a phantom
        improvement — so it is an error, not a failing metric, because a failing
        metric teaches people to override it.
        """
        with pytest.raises(DatasetMismatchError, match="Re-record"):
            check(
                suite="s",
                current={"accuracy": 0.99, "unknown_rate": 0.1},
                specs=self.SPECS,
                dataset_hash="different",
                baseline=self._baseline(accuracy=0.8, unknown_rate=0.2),
            )

    def test_drop_beyond_tolerance_regresses(self) -> None:
        report = check(
            suite="s",
            current={"accuracy": 0.70, "unknown_rate": 0.2},
            specs=self.SPECS,
            dataset_hash="abc123",
            baseline=self._baseline(accuracy=0.90, unknown_rate=0.2),
        )
        assert not report.passed
        assert report.verdicts[0].verdict is Verdict.REGRESSED

    def test_drop_within_tolerance_passes(self) -> None:
        """Sampling a model is noisy; the gate fires on drift, not jitter."""
        report = check(
            suite="s",
            current={"accuracy": 0.89, "unknown_rate": 0.2},
            specs=self.SPECS,
            dataset_hash="abc123",
            baseline=self._baseline(accuracy=0.90, unknown_rate=0.2),
        )
        assert report.passed

    def test_direction_is_respected(self) -> None:
        """unknown_rate rising is a regression even though the number went up."""
        report = check(
            suite="s",
            current={"accuracy": 0.90, "unknown_rate": 0.40},
            specs=self.SPECS,
            dataset_hash="abc123",
            baseline=self._baseline(accuracy=0.90, unknown_rate=0.20),
        )
        assert not report.passed
        assert report.verdicts[1].verdict is Verdict.REGRESSED

    def test_absolute_ceiling_fails_even_without_a_regression(self) -> None:
        """ "No worse than last time" is not the same as acceptable."""
        report = check(
            suite="s",
            current={"accuracy": 0.90, "unknown_rate": 0.60},
            specs=self.SPECS,
            dataset_hash="abc123",
            baseline=self._baseline(accuracy=0.90, unknown_rate=0.59),
        )
        assert not report.passed
        assert report.verdicts[1].verdict is Verdict.ABOVE_CEILING

    def test_first_run_records_rather_than_failing(self) -> None:
        report = check(
            suite="s",
            current={"accuracy": 0.9, "unknown_rate": 0.1},
            specs=self.SPECS,
            dataset_hash="abc123",
            baseline=None,
        )
        assert report.passed
        assert all(v.verdict is Verdict.NEW for v in report.verdicts)

    def test_unreported_metric_is_an_error(self) -> None:
        with pytest.raises(KeyError, match="unknown_rate"):
            check(
                suite="s",
                current={"accuracy": 0.9},
                specs=self.SPECS,
                dataset_hash="abc123",
                baseline=None,
            )


class TestRunner:
    def test_empty_split_is_an_error(self) -> None:
        """A suite that scores nothing would otherwise report a perfect run."""
        suite = Suite(name="s", dataset=_dataset(), specs=(), split=Split.HOLDOUT)
        with pytest.raises(ValueError, match="0 examples"):
            run(suite, diagnosis.rule_predictor, prompt_version="v1", model_id="m")

    def test_run_carries_the_dataset_hash(self) -> None:
        suite = diagnosis.build()
        result = run(suite, diagnosis.rule_predictor, prompt_version="v1", model_id="deterministic")
        assert result.dataset_hash == suite.dataset.content_hash
        assert len(result.predictions) == len(suite.examples())


class TestDiagnosisSuite:
    """The fixture dataset and the rule pre-check, checked together."""

    def test_dataset_loads(self) -> None:
        dataset = diagnosis.load_dataset()
        assert len(dataset.examples) == 30
        assert "unknown" in dataset.labels()

    def test_rule_precheck_never_guesses(self) -> None:
        """§3.1: a rule either matches exactly or abstains. Any confident answer
        it gives must be right, on every example in the dataset."""
        dataset = diagnosis.load_dataset()
        for example in dataset.examples:
            prediction = diagnosis.rule_predictor(example)
            if prediction.label != "unknown":
                assert prediction.label == example.expected, example.id

    def test_rule_precheck_covers_a_useful_share(self) -> None:
        """§3.1 claims the pre-check handles the common cases and skips the LLM
        for them — the cost lever only exists if coverage is real."""
        dataset = diagnosis.load_dataset()
        answered = sum(
            1 for e in dataset.examples if diagnosis.rule_predictor(e).label != "unknown"
        )
        assert answered / len(dataset.examples) >= 0.4
