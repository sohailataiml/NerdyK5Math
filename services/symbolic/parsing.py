"""Parsing untrusted math expressions (M0.7).

This module is the security boundary of the whole system. Implementation-Plan.md
M0.7 calls it out: the symbolic service is "where untrusted student input reaches
an expression evaluator", and SymPy's parser ultimately compiles and evaluates
the string it is given. `sympify` on attacker-controlled text is a documented
code-execution risk, and `parse_expr` is safer but not a sandbox.

So the defence is **allowlist before parse, never blocklist after**. Input must
consist only of characters and tokens that appear in K-12 arithmetic; anything
else is rejected without ever reaching SymPy. Two further limits stop the inputs
that are syntactically innocent but computationally hostile:

- a length cap, because parse cost grows with input size, and
- an exponent cap, because `9**9**9` is eleven characters that will exhaust
  memory before it finishes. This is the attack that a character allowlist alone
  would let straight through, and the one most likely to arrive by accident from
  a bored ten-year-old.

Everything here is pure and fast, so it is unit-testable without a server and
runs before any evaluation happens.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_LENGTH = 200
"""Longest accepted expression. A K-12 answer is a handful of characters; this is
generous by two orders of magnitude and still bounds parse cost."""

MAX_EXPONENT = 1000
"""Largest literal exponent. Above this, evaluation cost stops being bounded by
anything the student could reasonably have meant."""

MAX_POWER_OPERATORS = 1
"""Nested exponentiation (`2**3**4`) is a tower, not arithmetic. One is plenty
for K-12; two is an expression bomb."""

# Digits, the four operators, exponent, parens, decimal point, comparison-free
# whitespace, and single-letter variables for simple algebra. Deliberately no
# underscores, no dots-as-attribute-access, no commas, no brackets, no quotes.
_ALLOWED = re.compile(r"^[0-9a-zA-Z+\-*/^().\s]*$")

_ALLOWED_NAMES = frozenset("abcnmxyzt")
"""Single-letter variables that may appear. Anything longer is a function name or
an attribute lookup, and neither belongs in a student's arithmetic answer."""

_WORD = re.compile(r"[a-zA-Z_][a-zA-Z_0-9]*")
_POWER = re.compile(r"\*\*|\^")
_EXPONENT_LITERAL = re.compile(r"(?:\*\*|\^)\s*(\d+)")


class RejectedExpressionError(ValueError):
    """Input refused before parsing. Carries a reason safe to log."""


@dataclass(frozen=True)
class SafeExpression:
    """Text that passed every pre-parse check."""

    text: str


def _reject(reason: str) -> RejectedExpressionError:
    return RejectedExpressionError(reason)


def validate(raw: str) -> SafeExpression:
    """Check an expression is safe to hand to SymPy.

    Raises `RejectedExpressionError` with a specific reason rather than returning a
    boolean, so a rejection is never mistaken for "not equivalent" — the two
    have very different meanings for a student's grade.
    """
    text = raw.strip()
    if not text:
        raise _reject("expression is empty")
    if len(text) > MAX_LENGTH:
        raise _reject(f"expression is longer than {MAX_LENGTH} characters")
    if not _ALLOWED.match(text):
        bad = sorted({c for c in text if not _ALLOWED.match(c)})
        raise _reject(f"expression contains disallowed character(s): {bad}")

    for word in _WORD.findall(text):
        if len(word) > 1 or word not in _ALLOWED_NAMES:
            # Catches `__import__`, `os`, `sin`, `pi`, `E` — every name-based
            # route into the evaluator, including harmless-looking ones.
            raise _reject(f"expression contains an unsupported name: {word!r}")

    powers = len(_POWER.findall(text))
    if powers > MAX_POWER_OPERATORS:
        raise _reject(f"expression has {powers} exponent operators; at most {MAX_POWER_OPERATORS}")

    for exponent in _EXPONENT_LITERAL.findall(text):
        if int(exponent) > MAX_EXPONENT:
            raise _reject(f"exponent {exponent} exceeds the limit of {MAX_EXPONENT}")

    # Digit runs are how a small string becomes a huge number without any
    # operator at all (a 5,000-digit literal).
    for run in re.findall(r"\d+", text):
        if len(run) > 30:
            raise _reject(f"number with {len(run)} digits is longer than expected for K-12 work")

    return SafeExpression(text=text)
