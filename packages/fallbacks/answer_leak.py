"""Deterministic answer-leak detection (§3.3 layer 1, M0.12).

Architecture.md §3.3 puts two layers between a hint and a child: a deterministic
check for the answer written out, and a classifier for the implicit case. This is
the first layer — and, per §4, the one the leak-check stage falls back to when the
provider is down. It is also what validates the template library: a template is
only safe if filling it with a problem's values cannot render that problem's
answer.

The hard part is that "the answer" has many written forms. A hint that says
"twelve" leaks the answer to `7 + 5` exactly as surely as one that says "12", and
`0.5`, `1/2` and `2/4` are the same number. Matching the literal string would pass
all three.

Deliberately biased toward false positives. §12 makes leakage the defining risk
of this architecture: a hint wrongly held back is regenerated for a fraction of a
cent, and a leak reaches a child and cannot be taken back.
"""

from __future__ import annotations

import re
from fractions import Fraction

_UNITS = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
}
_TENS = {
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
    "hundred": 100,
}

NUMBER_WORDS: dict[str, Fraction] = {
    **{word: Fraction(value) for word, value in _UNITS.items()},
    **{word: Fraction(value) for word, value in _TENS.items()},
    # Fraction words a K-5 hint might reach for.
    "half": Fraction(1, 2),
    "quarter": Fraction(1, 4),
    "third": Fraction(1, 3),
    "three quarters": Fraction(3, 4),
    "two thirds": Fraction(2, 3),
}

_NUMBER = re.compile(r"\d+(?:\.\d+)?(?:\s*/\s*\d+)?")


def parse_number(text: str) -> Fraction | None:
    """Read a written number in any of the forms a K-12 answer takes."""
    cleaned = text.strip().lower().replace(",", "")
    if not cleaned:
        return None

    if "/" in cleaned:
        left, _, right = cleaned.partition("/")
        try:
            return Fraction(int(left.strip()), int(right.strip()))
        except (ValueError, ZeroDivisionError):
            return None
    try:
        return Fraction(cleaned)
    except ValueError:
        pass
    return NUMBER_WORDS.get(cleaned)


def numbers_in(text: str) -> set[Fraction]:
    """Every number the text states, in digits or in words.

    Word forms are checked as one- and two-word runs so "three quarters" is read
    as 3/4 rather than as 3 and 4 separately.
    """
    found: set[Fraction] = set()
    lowered = text.lower()

    for match in _NUMBER.finditer(lowered):
        value = parse_number(match.group())
        if value is not None:
            found.add(value)

    words = re.findall(r"[a-z]+", lowered)
    for index, word in enumerate(words):
        if word in NUMBER_WORDS:
            found.add(NUMBER_WORDS[word])
        if index + 1 < len(words):
            pair = f"{word} {words[index + 1]}"
            if pair in NUMBER_WORDS:
                found.add(NUMBER_WORDS[pair])
    return found


class LeakVerdict:
    """Whether a hint gives the answer away, and why."""

    __slots__ = ("leaked", "reason")

    def __init__(self, leaked: bool, reason: str | None = None) -> None:
        self.leaked = leaked
        self.reason = reason

    def __bool__(self) -> bool:
        return self.leaked

    def __repr__(self) -> str:
        return f"LeakVerdict(leaked={self.leaked}, reason={self.reason!r})"


SAFE = LeakVerdict(False)

CHECKER_VERSION = "deterministic/v1"
"""Recorded on every hint (§5 `HintLog.leak_checker_version`).

When the checker is revised, this identifies which hints were cleared by the
older logic — without it, a corpus expansion cannot tell you what to re-examine.
"""


def check_hint(hint_text: str, correct_answer: str) -> LeakVerdict:
    """Does this hint state the answer?

    Numeric equality, not string matching: "twelve", "12", and "12.0" are the
    same leak, and a hint containing `1/2` gives away an answer of `0.5`.
    """
    answer = parse_number(correct_answer)
    if answer is None:
        # A non-numeric expected answer (an algebraic form, a written
        # explanation) is outside this layer's competence. Saying "safe" would
        # be a guess dressed as a verdict, so it fails closed instead.
        if correct_answer.strip().lower() in hint_text.lower():
            return LeakVerdict(True, f"hint contains the answer text {correct_answer!r}")
        return LeakVerdict(
            True,
            f"cannot verify a non-numeric answer {correct_answer!r} deterministically; "
            f"escalate to the classifier layer",
        )

    stated = numbers_in(hint_text)
    if answer in stated:
        return LeakVerdict(True, f"hint states the answer ({correct_answer})")
    return SAFE


def check_template_safety(
    rendered: str, correct_answer: str, *, problem_values: dict[str, str] | None = None
) -> LeakVerdict:
    """Check a filled template.

    The subtle failure the deterministic revision names: a template that is safe
    as written can leak once a *particular* problem's values are substituted into
    it. "Start at {a} and count on" is fine for 7 + 5 and gives the game away on a
    problem whose answer happens to equal `a`. Authoring-time review cannot catch
    that, because the values are not present at authoring time.
    """
    verdict = check_hint(rendered, correct_answer)
    if verdict.leaked and problem_values:
        answer = parse_number(correct_answer)
        colliding = [
            name
            for name, value in problem_values.items()
            if answer is not None and parse_number(value) == answer
        ]
        if colliding:
            return LeakVerdict(
                True,
                f"slot(s) {sorted(colliding)} hold a value equal to the answer "
                f"({correct_answer}) for this problem",
            )
    return verdict
