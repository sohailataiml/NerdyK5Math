"""Pre-authored template hints (§3.3 fallback, M0.12).

The student-facing degradation path, and — per Implementation-Plan.md §10 — what
Phase 0 actually serves children with while the generator runs in shadow. So this
is not a stub: for the duration of Phase 0 these *are* the hints.

Two things follow from that. Every template is written for a child to read rather
than as placeholder text. And every one is checked against the answer at render
time, because §3.3's structural guarantee ("answer-leakage is prevented by review
at authoring time") has a gap the prior revision names precisely: a template that
is safe as written can leak once a particular problem's values are substituted.

Variety is deliberate. §12 flags staleness — a child hitting the same
misconception twice gets the identical words both times, with nothing paraphrasing
them — so each level carries two or three phrasings, rotated on attempt number.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from packages.domain.enums import GradeBand
from packages.fallbacks.answer_leak import LeakVerdict, check_template_safety

MAX_HINT_LEVEL = 3

_SLOT = re.compile(r"\{(\w+)\}")


@dataclass(frozen=True)
class HintTemplate:
    tag: str
    grade_band: GradeBand
    level: int
    variants: tuple[str, ...]

    def slots(self) -> frozenset[str]:
        return frozenset(slot for v in self.variants for slot in _SLOT.findall(v))


# Level 1 asks a question, level 2 works the first step, level 3 works everything
# but the last step (§3.3). None of them names the answer.
LIBRARY: tuple[HintTemplate, ...] = (
    HintTemplate(
        tag="subtracted_instead_of_added",
        grade_band=GradeBand.K_1,
        level=1,
        variants=(
            "You have {a} counters and you're getting {b} more. Are you ending up "
            "with more than {a}, or fewer?",
            "Look at the sign between {a} and {b}. Is it asking you to put them "
            "together, or take some away?",
        ),
    ),
    HintTemplate(
        tag="subtracted_instead_of_added",
        grade_band=GradeBand.K_1,
        level=2,
        variants=(
            "Start with {a} counters. Now put {b} more next to them. Count all of "
            "them — how many is that?",
            "Fill a ten-frame with {a}. Then add {b} more counters. What do you have altogether?",
        ),
    ),
    HintTemplate(
        tag="subtracted_instead_of_added",
        grade_band=GradeBand.K_1,
        level=3,
        variants=(
            "Put {a} counters down, then {b} more. Count them one at a time, "
            "starting from {a}: that's {a}, then keep going. What number do you "
            "land on?",
        ),
    ),
    HintTemplate(
        tag="added_instead_of_subtracted",
        grade_band=GradeBand.K_1,
        level=1,
        variants=(
            "You start with {a} and take {b} away. Should you end up with more than {a}, or fewer?",
            "The sign between {a} and {b} is a take-away sign. What does that mean "
            "for your answer?",
        ),
    ),
    HintTemplate(
        tag="added_instead_of_subtracted",
        grade_band=GradeBand.K_1,
        level=2,
        variants=(
            "Put out {a} counters. Now take {b} of them away. How many are left?",
            "Start at {a} on a number line and hop back {b} times. Where do you stop?",
        ),
    ),
    HintTemplate(
        tag="added_instead_of_subtracted",
        grade_band=GradeBand.K_1,
        level=3,
        variants=(
            "Start at {a} and count backwards {b} times, one hop each. Say each "
            "number out loud as you go. Where do you finish?",
        ),
    ),
    HintTemplate(
        tag="counted_on_from_wrong_start",
        grade_band=GradeBand.K_1,
        level=1,
        variants=(
            "When you counted on from {a}, did you say {a} as your first count, or "
            "the number after it?",
            "You're very close. Check where your counting started — should {a} "
            "itself be one of the hops?",
        ),
    ),
    HintTemplate(
        tag="counted_on_from_wrong_start",
        grade_band=GradeBand.K_1,
        level=2,
        variants=(
            "Put your finger on {a}. Now make {b} hops forward — the first hop "
            "lands on the number *after* {a}. Where do you end up?",
        ),
    ),
    HintTemplate(
        tag="counted_on_from_wrong_start",
        grade_band=GradeBand.K_1,
        level=3,
        variants=(
            "Start at {a}. Hop forward once and you're at {a} plus one. Keep "
            "hopping until you've made {b} hops in total. What's the last number "
            "you say?",
        ),
    ),
    # The `unknown` path: no rule fired, so the hint cannot assume a
    # misconception. §3.1 routes here rather than guessing.
    HintTemplate(
        tag="unknown",
        grade_band=GradeBand.K_1,
        level=1,
        variants=(
            "Let's look at this one together. Can you show me how you worked it out?",
            "Tell me what you did first — I'd like to follow your thinking.",
        ),
    ),
    HintTemplate(
        tag="unknown",
        grade_band=GradeBand.K_1,
        level=2,
        variants=(
            "Try it with counters: put out {a}, then deal with the {b}. Talk me "
            "through what happens.",
        ),
    ),
    HintTemplate(
        tag="unknown",
        grade_band=GradeBand.K_1,
        level=3,
        variants=(
            "Let's slow down and do it one step at a time. Start with {a}. What is "
            "the very first thing the problem asks you to do?",
        ),
    ),
)


class TemplateError(RuntimeError):
    """No usable template, or the only candidate would leak the answer."""


@dataclass(frozen=True)
class RenderedTemplate:
    text: str
    tag: str
    level: int
    variant_index: int


def find(tag: str, grade_band: GradeBand, level: int) -> HintTemplate | None:
    for template in LIBRARY:
        if template.tag == tag and template.grade_band is grade_band and template.level == level:
            return template
    return None


def render(
    *,
    tag: str,
    grade_band: GradeBand,
    level: int,
    values: dict[str, str],
    correct_answer: str,
    attempt: int = 1,
) -> RenderedTemplate:
    """Fill a template and refuse to return one that gives the answer away.

    Variants rotate on `attempt` so a child meeting the same misconception twice
    does not get the identical sentence (§12 staleness).
    """
    level = max(1, min(level, MAX_HINT_LEVEL))
    template = find(tag, grade_band, level) or find("unknown", grade_band, level)
    if template is None:
        raise TemplateError(
            f"no template for tag={tag!r} band={grade_band.value} level={level}, "
            f"and no 'unknown' template to fall back to"
        )

    missing = template.slots() - set(values)
    if missing:
        raise TemplateError(f"template {tag}/{level} needs slot(s) {sorted(missing)}")

    # Try each phrasing; a leak in one is not a leak in all, because variants
    # mention different values.
    rejected: list[LeakVerdict] = []
    for offset in range(len(template.variants)):
        index = (attempt - 1 + offset) % len(template.variants)
        text = _SLOT.sub(lambda m: values[m.group(1)], template.variants[index])
        verdict = check_template_safety(text, correct_answer, problem_values=values)
        if not verdict.leaked:
            return RenderedTemplate(text=text, tag=template.tag, level=level, variant_index=index)
        rejected.append(verdict)

    # Every phrasing would leak. Refusing is correct: §12 makes leakage the
    # defining risk, and there is no version of "show it anyway" that is better
    # than escalating.
    raise TemplateError(
        f"every variant of {template.tag}/{level} would leak the answer for this "
        f"problem: {[v.reason for v in rejected]}"
    )
