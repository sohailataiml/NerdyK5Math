"""The deterministic diagnosis pre-check (§3.1, M0.12).

Architecture.md §3.1 keeps this in front of the LLM diagnoser — "not because the
model can't handle it, but because it's free, instant, and perfectly precise on
the ~40% of cases it covers." It is also the degradation path §4 requires: when
the provider is slow or down, diagnosis falls back here rather than going dark.

Every rule is an exact arithmetic identity, so a match is certain. Anything a
rule does not recognise returns `unknown` and falls through — this layer never
guesses, because a confident wrong tag sends a student a hint aimed at a
misconception they do not have (§12).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from packages.domain.enums import UNKNOWN_TAG_LABEL, Operation

_PROBLEM = re.compile(r"(?P<left>\d+)\s*(?P<op>[+\-])\s*(?P<right>\d+)")

RULE_CONFIDENCE = 0.99
"""Exact identities, so near-certain — but not 1.0.

Reserving 1.0 keeps the scale honest: an arithmetic coincidence can make a rule
fire on an answer the student reached another way, and a metric that reports
perfect certainty invites treating the pre-check as ground truth rather than as
strong evidence.
"""


@dataclass(frozen=True)
class ParsedProblem:
    left: int
    right: int
    operation: Operation


@dataclass(frozen=True)
class Rule:
    """One error signature. `matches` receives the parsed problem and the
    student's numeric answer."""

    tag: str
    operation: Operation
    matches: Callable[[ParsedProblem, int], bool]
    description: str


def parse_problem(prompt: str) -> ParsedProblem | None:
    """Extract `a op b` from a problem prompt. Returns None if it isn't one."""
    match = _PROBLEM.search(prompt)
    if match is None:
        return None
    operation = Operation.ADDITION if match["op"] == "+" else Operation.SUBTRACTION
    return ParsedProblem(left=int(match["left"]), right=int(match["right"]), operation=operation)


RULES: tuple[Rule, ...] = (
    Rule(
        tag="subtracted_instead_of_added",
        operation=Operation.ADDITION,
        matches=lambda p, answer: answer == abs(p.left - p.right),
        description="answer equals |a - b| on an addition problem",
    ),
    Rule(
        tag="added_instead_of_subtracted",
        operation=Operation.SUBTRACTION,
        matches=lambda p, answer: answer == p.left + p.right,
        description="answer equals a + b on a subtraction problem",
    ),
    Rule(
        tag="counted_on_from_wrong_start",
        operation=Operation.ADDITION,
        matches=lambda p, answer: answer == p.left + p.right + 1,
        description="answer is one more than a + b (counted the start number)",
    ),
)


@dataclass(frozen=True)
class RuleDiagnosis:
    tag: str
    confidence: float
    evidence: str
    matched_rule: str | None


UNKNOWN = RuleDiagnosis(
    tag=UNKNOWN_TAG_LABEL, confidence=0.0, evidence="no rule matched", matched_rule=None
)


def diagnose(problem_prompt: str, student_answer: str) -> RuleDiagnosis:
    """Try every rule in order; first match wins."""
    parsed = parse_problem(problem_prompt)
    if parsed is None:
        return UNKNOWN

    try:
        answer = int(student_answer.strip())
    except ValueError:
        # Non-numeric answers are a real case (a child types words), and they are
        # exactly what the LLM path exists for.
        return UNKNOWN

    correct = (
        parsed.left + parsed.right
        if parsed.operation is Operation.ADDITION
        else parsed.left - parsed.right
    )
    if answer == correct:
        return UNKNOWN  # nothing to diagnose; the answer is right

    for index, rule in enumerate(RULES):
        if rule.operation is parsed.operation and rule.matches(parsed, answer):
            return RuleDiagnosis(
                tag=rule.tag,
                confidence=RULE_CONFIDENCE,
                evidence=f"{rule.description} ({parsed.left}, {parsed.right} -> {answer})",
                matched_rule=f"{parsed.operation.value}.rule_{index:02d}_{rule.tag}",
            )
    return UNKNOWN


TAG_OPERATIONS: dict[str, frozenset[Operation]] = {
    tag: frozenset(rule.operation for rule in RULES if rule.tag == tag)
    for tag in {rule.tag for rule in RULES}
}
"""Which operations each known misconception can occur on.

Derived from `RULES` rather than written out, so a rule and its applicability
cannot drift apart. The deterministic path already respects this — `diagnose`
only considers rules whose operation matches — and `applies_to` exists so the
*model* path can respect it too.

A tag absent from this map is unconstrained: the taxonomy is larger than the set
of errors expressible as exact arithmetic identities, and refusing every tag
without a rule would throw away the diagnoser's whole reason for existing.
"""


def applies_to(tag: str, prompt: str) -> bool:
    """Can this misconception occur on this problem at all?

    §3.1's constraint is that the diagnoser's output vocabulary is the taxonomy.
    That is necessary and not sufficient: `counted_on_from_wrong_start` is a
    valid tag and a nonsense diagnosis for `13 - 8`, and the hint that follows
    tells a child to make 8 hops *forward* from 13. A tag that cannot apply is a
    confident wrong diagnosis, which §12 rates worse than no diagnosis.
    """
    allowed = TAG_OPERATIONS.get(tag)
    if allowed is None:
        return True
    parsed = parse_problem(prompt)
    if parsed is None:
        # No operation to contradict. The check abstains rather than blocking a
        # tag on a problem it cannot read.
        return True
    return parsed.operation in allowed
