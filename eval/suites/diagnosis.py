"""The diagnosis suite (M0.5).

Metric specs encode Implementation-Plan.md's Phase 0 exit criteria directly, so
the gate enforces the plan rather than restating it in prose:

- `high_confidence_precision >= 0.90` — §8's calibration gate. A 0.9 that isn't
  right 90% of the time makes every downstream confidence threshold in §3.1
  meaningless, which is why this is a floor and not a trend.
- `expected_calibration_error <= 0.15` — the same property measured across the
  whole confidence range rather than only at the top.
- `unknown_rate` is capped rather than minimised. Abstention is correct
  behaviour under ambiguity (§3.1), so driving it to zero would mean the
  diagnoser had started guessing.

Two predictors are provided. `rule_predictor` is deterministic and free — it
establishes the baseline any model has to beat, which is the comparison Phase 0
actually needs (P0.5: is generation beating the deterministic path?).
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from pathlib import Path

from eval.harness.baseline import Direction, MetricSpec
from eval.harness.dataset import Dataset, Example, Split, load_jsonl
from eval.harness.metrics import Prediction
from eval.harness.runner import Suite
from packages.domain.enums import UNKNOWN_TAG_LABEL, GradeBand, PipelineStage
from packages.fallbacks.rules import diagnose
from packages.llm import LLMClient, PromptContext
from packages.prompts import PromptRegistry

DATASET_PATH = Path("eval/datasets/diagnosis/k1-arithmetic-v1.jsonl")

SPECS: tuple[MetricSpec, ...] = (
    MetricSpec(
        name="high_confidence_precision",
        direction=Direction.HIGHER_IS_BETTER,
        tolerance=0.03,
        minimum=0.90,
    ),
    MetricSpec(
        name="accuracy_when_answered",
        direction=Direction.HIGHER_IS_BETTER,
        tolerance=0.05,
    ),
    MetricSpec(
        name="macro_f1",
        direction=Direction.HIGHER_IS_BETTER,
        tolerance=0.05,
    ),
    MetricSpec(
        name="expected_calibration_error",
        direction=Direction.LOWER_IS_BETTER,
        tolerance=0.05,
        maximum=0.15,
    ),
    MetricSpec(
        name="unknown_rate",
        direction=Direction.LOWER_IS_BETTER,
        tolerance=0.10,
        maximum=0.70,
    ),
)


def load_dataset(path: Path = DATASET_PATH) -> Dataset:
    return load_jsonl(path)


def build(dataset: Dataset | None = None, split: Split = Split.HOLDOUT) -> Suite:
    return Suite(
        name="diagnosis",
        dataset=dataset if dataset is not None else load_dataset(),
        specs=SPECS,
        split=split,
    )


def rule_predictor(example: Example) -> Prediction:
    """The deterministic §3.1 pre-check. No model call, no cost, no key."""
    result = diagnose(example.inputs["problem"], example.inputs["student_answer"])
    return Prediction(
        example_id=example.id,
        label=result.tag,
        confidence=result.confidence,
    )


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def parse_diagnosis(text: str) -> tuple[str, float]:
    """Read the §3.1 JSON contract out of a model response.

    Models wrap JSON in code fences often enough that stripping them is part of
    parsing, not a workaround. A response that still will not parse is scored as
    `unknown` at zero confidence rather than raising: an unparseable answer is a
    real failure mode the metrics should show, not an exception that aborts the
    run and hides how often it happens.
    """
    try:
        payload = json.loads(_FENCE.sub("", text).strip())
        tag = str(payload["tag"])
        confidence = float(payload.get("confidence", 0.0))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return UNKNOWN_TAG_LABEL, 0.0
    return tag, max(0.0, min(1.0, confidence))


DEFAULT_PROMPT_VERSION = "v2"
"""v1 over-abstained: it returned `unknown` on every `subtracted_instead_of_added`
case, the most common misconception in the set. The eval gate caught it, v1 stayed
published and immutable, and v2 rebalanced the abstention guidance."""


def llm_predictor(
    client: LLMClient, *, band: str = "K-1", version: str = DEFAULT_PROMPT_VERSION
) -> Callable[[Example], Prediction]:
    """Run the real diagnose prompt. Needs a key; costs a fraction of a cent per example."""
    registry = PromptRegistry()
    session_id = uuid.uuid4()  # one synthetic session for the whole eval run

    def predict(example: Example) -> Prediction:
        prompt = registry.render(
            stage=PipelineStage.DIAGNOSE,
            band=band,
            version=version,
            values={
                "problem": example.inputs["problem"],
                "correct_answer": example.inputs["correct_answer"],
                "student_answer": example.inputs["student_answer"],
            },
        )
        context = PromptContext(
            session_id=session_id,
            grade_band=GradeBand(example.inputs["grade_band"]),
            problem_prompt=example.inputs["problem"],
            correct_answer=example.inputs["correct_answer"],
            student_answer=example.inputs["student_answer"],
        )
        result = client.complete(stage=PipelineStage.DIAGNOSE, context=context, prompt=prompt)
        tag, confidence = parse_diagnosis(result.text)
        return Prediction(example_id=example.id, label=tag, confidence=confidence)

    return predict
