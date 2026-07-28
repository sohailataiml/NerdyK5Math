"""The measured false-negative rate for the §7 welfare screen (P1.8).

P1.8 does not ask for distress screening. It asks for distress screening *with a
measured false-negative rate*, and the difference is the whole requirement: a
screen nobody has measured is a claim, and the claim is about children.

`tests/test_adversarial.py::TestDistressCorpus` already gates layer 1 in CI for
free. This runs the full two-layer screen, which needs the model, so it lives
here rather than in pytest — the same split the diagnosis suite uses, and for the
same reason: a billed, nondeterministic assertion in the unit suite becomes a
flaky test, and a flaky test becomes a deleted one.

**The number this prints is not a safety certificate.** The corpus is
engineer-written and inadequate by construction (see the file's own header). What
the number is good for is comparison: between layer 1 alone and both layers,
between prompt versions, and against itself over time. An absolute claim needs a
counsellor-authored corpus, and this reports which one it ran on so the two can
never be confused.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from packages.domain.enums import GradeBand, PipelineStage
from packages.fallbacks.distress import screen
from packages.llm import LLMClient, PromptContext
from packages.llm.errors import RefusalError, TransportError
from packages.prompts import PromptRegistry

CORPUS_PATH = Path("eval/adversarial/distress_corpus.jsonl")


@dataclass(frozen=True)
class Case:
    id: str
    category: str
    layer: str
    text: str

    @property
    def is_signal(self) -> bool:
        return self.category != "CLEAR"


@dataclass(frozen=True)
class Outcome:
    case: Case
    flagged: bool
    predicted: str | None
    layer_one_flagged: bool
    classifier_ran: bool

    @property
    def is_false_negative(self) -> bool:
        return self.case.is_signal and not self.flagged

    @property
    def is_false_positive(self) -> bool:
        return not self.case.is_signal and self.flagged

    @property
    def is_miscategorised(self) -> bool:
        """Caught, but routed to the wrong kind of adult.

        Counted apart from a miss because it is a much smaller failure — a person
        still reads it — but not folded into "correct" either, since urgency is
        what decides whether it is read today or on Friday.
        """
        return self.case.is_signal and self.flagged and self.predicted != self.case.category


def load_corpus(path: Path = CORPUS_PATH) -> tuple[str, tuple[Case, ...]]:
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    header = json.loads(lines[0])
    cases = tuple(
        Case(id=row["id"], category=row["category"], layer=row["layer"], text=row["text"])
        for row in (json.loads(line) for line in lines[1:])
    )
    return f"{header['name']} {header['version']}", cases


def run_case(case: Case, client: LLMClient | None, prompts: PromptRegistry) -> Outcome:
    """Both layers, in the order the pipeline runs them."""
    deterministic = screen(case.text)
    if deterministic.flagged:
        return Outcome(
            case=case,
            flagged=True,
            predicted=deterministic.category.value if deterministic.category else None,
            layer_one_flagged=True,
            classifier_ran=False,
        )
    if client is None:
        return Outcome(
            case=case, flagged=False, predicted=None, layer_one_flagged=False, classifier_ran=False
        )

    rendered = prompts.render(
        stage=PipelineStage.SAFETY_SCREEN,
        band="shared",
        version="v1",
        values={"student_text": case.text},
    )
    try:
        result = client.complete(
            stage=PipelineStage.SAFETY_SCREEN,
            context=PromptContext(
                session_id=uuid.uuid4(),
                grade_band=GradeBand.K_1,
                problem_prompt="(eval)",
                student_answer=case.text,
            ),
            prompt=rendered,
        )
    except (TransportError, RefusalError):
        # Matches the stage: an unreachable classifier is the layer not running,
        # not a verdict. Scored as a miss so an outage cannot flatter the number.
        return Outcome(
            case=case, flagged=False, predicted=None, layer_one_flagged=False, classifier_ran=False
        )

    verdict = result.text.strip().upper()
    if verdict.startswith("CLEAR"):
        return Outcome(
            case=case, flagged=False, predicted=None, layer_one_flagged=False, classifier_ran=True
        )
    predicted = next(
        (
            name.lower()
            for name in ("SELF_HARM", "HARM_FROM_OTHERS", "HOPELESSNESS")
            if name in verdict
        ),
        None,
    )
    return Outcome(
        case=case,
        flagged=predicted is not None,
        predicted=predicted,
        layer_one_flagged=False,
        classifier_ran=True,
    )


@dataclass(frozen=True)
class Report:
    corpus: str
    outcomes: tuple[Outcome, ...]
    with_classifier: bool

    @property
    def signals(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.case.is_signal]

    @property
    def clears(self) -> list[Outcome]:
        return [o for o in self.outcomes if not o.case.is_signal]

    @property
    def false_negatives(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.is_false_negative]

    @property
    def false_positives(self) -> list[Outcome]:
        return [o for o in self.outcomes if o.is_false_positive]

    @property
    def false_negative_rate(self) -> float:
        return len(self.false_negatives) / len(self.signals) if self.signals else 0.0

    @property
    def false_positive_rate(self) -> float:
        return len(self.false_positives) / len(self.clears) if self.clears else 0.0

    def render(self) -> str:
        layers = "patterns + classifier" if self.with_classifier else "patterns only"
        lines = [
            "",
            f"=== distress screen — {layers} ===",
            "",
            f"  corpus: {self.corpus}  ({len(self.signals)} signal, {len(self.clears)} clear)",
            "",
            f"  false-negative rate   {self.false_negative_rate:.1%}  "
            f"({len(self.false_negatives)}/{len(self.signals)} signals missed)",
            f"  false-positive rate   {self.false_positive_rate:.1%}  "
            f"({len(self.false_positives)}/{len(self.clears)} ordinary texts flagged)",
        ]

        by_category = Counter(o.case.category for o in self.false_negatives)
        if by_category:
            lines.append("")
            lines.append("  missed, by category:")
            for category, count in sorted(by_category.items()):
                total = sum(1 for o in self.signals if o.case.category == category)
                lines.append(f"    {category:<20} {count}/{total}")

        misrouted = [o for o in self.outcomes if o.is_miscategorised]
        if misrouted:
            lines.append("")
            lines.append(f"  caught but misrouted: {len(misrouted)}")
            for outcome in misrouted:
                lines.append(
                    f"    {outcome.case.id:<28} {outcome.case.category} -> {outcome.predicted}"
                )

        if self.false_negatives:
            lines.append("")
            lines.append("  missed:")
            for outcome in self.false_negatives:
                lines.append(f"    {outcome.case.id:<28} {outcome.case.text!r}")

        if self.false_positives:
            lines.append("")
            lines.append("  false alarms (the alert-fatigue cost):")
            for outcome in self.false_positives:
                lines.append(f"    {outcome.case.id:<28} {outcome.case.text!r}")

        lines += [
            "",
            "  This corpus is engineer-written and inadequate by construction.",
            "  The number is for comparison between layers, prompt versions, and",
            "  itself over time — not a claim that children are covered.",
            "",
        ]
        return "\n".join(lines)


def build_report(client: LLMClient | None) -> Report:
    corpus, cases = load_corpus()
    prompts = PromptRegistry()
    outcomes = tuple(run_case(case, client, prompts) for case in cases)
    return Report(corpus=corpus, outcomes=outcomes, with_classifier=client is not None)
