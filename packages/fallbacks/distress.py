"""Deterministic distress screening — layer 1 of the §7 safety screen (P1.8).

Student answers are free text typed by children. A child in trouble does not
wait for the phase in which we planned to look, so this runs on every submission
from the moment a real student touches the system, including throughout Phase 0
shadow mode.

**This module does not detect distress. It detects phrases.** The distinction is
the whole design. A pattern match is a routing decision — get this in front of an
adult — never a conclusion about a child, and nothing downstream is permitted to
treat it as one. `screen` returns what matched and why; a human decides what it
means.

Two limits, stated here rather than discovered during a pilot:

1. **These patterns are engineer-written and are not adequate.** They must be
   reviewed and replaced by a school counsellor before real students use the
   system, exactly as the misconception taxonomy must be authored with a teacher.
   The failure mode is different in kind, though: a wrong misconception tag
   produces a bad hint, and a missed distress signal produces nothing at all —
   the system looks like it is working right up until it matters.
2. **Children in K-5 spell badly.** "kil myself", "i wanna dye", and everything
   in between defeat a word-boundary regex. That is not a fixable weakness of
   this layer; it is the argument for layer 2 (the classifier) and the reason
   P1.8 requires a *measured* false-negative rate rather than a plausible-looking
   pattern list.

On the false-positive tension: over-flagging costs a teacher ten seconds, and
under-flagging can cost something that cannot be undone, so the patterns lean
toward recall. But alert fatigue destroys recall in practice — a teacher who
dismisses forty false alarms will dismiss the forty-first without reading it. The
resolution is not a cleverer threshold. It is that the alert is cheap to triage
and a human, not this module, is the decider. Hence the deliberate refusal to
match bare violent idiom: children say "this problem is killing me" constantly,
and a screen that fires on it is a screen nobody reads.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from packages.domain.enums import URGENT_SAFETY_CATEGORIES, SafetyCategory

SCREENER_VERSION = "distress/deterministic/v1"


@dataclass(frozen=True)
class Signal:
    pattern: re.Pattern[str]
    category: SafetyCategory
    description: str


def _signal(source: str, category: SafetyCategory, description: str) -> Signal:
    return Signal(re.compile(source, re.IGNORECASE), category, description)


SIGNALS: tuple[Signal, ...] = (
    # Self-harm. Every pattern here requires a self-referential subject: the
    # object of the sentence has to be the child. Bare violent vocabulary is
    # excluded on purpose (see the module docstring).
    _signal(
        r"\b(?:i|im|i'?m)\s+(?:want|wanna|wish|going|gonna)\s*(?:to|ta)?\s*(?:die|be dead)\b",
        SafetyCategory.SELF_HARM,
        "wanting to die",
    ),
    _signal(
        r"\b(?:kill|hurt|cut|harm)(?:ing)?\s+(?:my\s*self|myself)\b",
        SafetyCategory.SELF_HARM,
        "hurting oneself",
    ),
    _signal(
        # "i wish i could disappear" is as common as "i want to disappear" at
        # this age, so the optional middle covers both rather than only the
        # phrasing that happened to be written first.
        r"\bi\s+(?:want|wanna|wish)\s+(?:to\s+|i\s+could\s+)?"
        r"(?:disappear|not exist|go away forever)\b",
        SafetyCategory.SELF_HARM,
        "wanting to disappear",
    ),
    _signal(
        r"\b(?:nobody|no ?one)\s+(?:would|will)\s+(?:miss|care about)\s+me\b",
        SafetyCategory.SELF_HARM,
        "believing they would not be missed",
    ),
    # Harm from others.
    _signal(
        r"\b(?:hits?|hitting|hurts?|hurting|beats?|beating|touch(?:es|ed|ing)?)\s+me\b",
        SafetyCategory.HARM_FROM_OTHERS,
        "being hurt by someone",
    ),
    _signal(
        r"\b(?:scared|afraid|frightened)\s+(?:to\s+go\s+home|of\s+(?:my|going home))\b",
        SafetyCategory.HARM_FROM_OTHERS,
        "fear of home",
    ),
    _signal(
        r"\b(?:i'?m\s+)?not\s+safe\b",
        SafetyCategory.HARM_FROM_OTHERS,
        "saying they are not safe",
    ),
    # Hopelessness.
    _signal(
        r"\bi\s+(?:hate|h8)\s+(?:my ?self|me)\b",
        SafetyCategory.HOPELESSNESS,
        "self-directed hate",
    ),
    _signal(
        r"\b(?:everyone|everybody)\s+(?:hates|would be better without)\s+me\b",
        SafetyCategory.HOPELESSNESS,
        "believing they are unwanted",
    ),
)

_URGENCY = {
    SafetyCategory.SELF_HARM: 0,
    SafetyCategory.HARM_FROM_OTHERS: 1,
    SafetyCategory.HOPELESSNESS: 2,
}


@dataclass(frozen=True)
class DistressVerdict:
    """What matched, if anything. Not a judgement about the child."""

    flagged: bool
    category: SafetyCategory | None = None
    description: str | None = None
    matched_text: str | None = None
    screener_version: str = SCREENER_VERSION

    @property
    def is_urgent(self) -> bool:
        """Self-harm and harm-from-others need an adult now, not at end of day."""
        return self.category in URGENT_SAFETY_CATEGORIES


CLEAR = DistressVerdict(flagged=False)


def screen(text: str) -> DistressVerdict:
    """Screen one piece of student text.

    Returns the *most urgent* match rather than the first, so a message
    containing both hopelessness and a self-harm statement routes on the
    self-harm. Ordering by position in the string would make urgency depend on
    sentence order, which is not a property of the child's situation.
    """
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return CLEAR

    matches = [
        (signal, found)
        for signal in SIGNALS
        if (found := signal.pattern.search(normalized)) is not None
    ]
    if not matches:
        return CLEAR

    signal, found = min(matches, key=lambda pair: _URGENCY[pair[0].category])
    return DistressVerdict(
        flagged=True,
        category=signal.category,
        description=signal.description,
        matched_text=found.group(0),
    )
