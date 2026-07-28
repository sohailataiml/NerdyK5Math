"""Mathematical equivalence checking (M0.7).

Architecture.md §3.5: "`4/8` and `1/2` both grade correct" — the check is
mathematical, not textual. The model normalizes messy student input into an
expression; this decides whether that expression equals the expected one, and
when the two disagree the checker wins.

**What this deliberately does not decide.** Whether an unsimplified answer is
*acceptable* is a curriculum question, not a mathematical one — §3.5 and P2.1 put
it in `Problem.answer_type` plus curriculum config, "not in the CAS". `4/8` is
right in grade 3 and wrong in grade 6, and this service cannot know which. So it
reports facts — equivalent, canonical form, whether the answer is in lowest terms
— and the grading stage applies the grade-band policy.
"""

from __future__ import annotations

from dataclasses import dataclass

import sympy
from sympy.parsing.sympy_parser import parse_expr, standard_transformations

from services.symbolic.parsing import RejectedExpressionError, validate

# No implicit multiplication, no auto-symbol creation beyond what the allowlist
# already permits: the minimum transformation set that still parses `1/2` and
# `2*x + 3`. Every extra transformation is more parser surface for hostile input.
_TRANSFORMATIONS = standard_transformations

_GLOBALS: dict[str, object] = {
    # parse_expr compiles the expression to Python source that calls these
    # constructors, so they must resolve. This is the complete set the standard
    # transformations emit — an allowlist, not SymPy's full namespace, which
    # would put `sympify`, `factorial` and several hundred other callables one
    # name away from student input.
    "Integer": sympy.Integer,
    "Float": sympy.Float,
    "Rational": sympy.Rational,
    "Symbol": sympy.Symbol,
}


@dataclass(frozen=True)
class EquivalenceResult:
    equivalent: bool
    expected_canonical: str
    actual_canonical: str
    actual_is_simplified: bool
    reason: str | None = None


class EvaluationError(RuntimeError):
    """Parsing succeeded the allowlist but SymPy could not make sense of it."""


def _parse(text: str) -> sympy.Expr:
    safe = validate(text)
    # `^` means exponent to a student, XOR to Python. Rewriting it here rather
    # than enabling SymPy's convert_xor transformation keeps the transformation
    # set minimal.
    prepared = safe.text.replace("^", "**")
    try:
        expression = parse_expr(
            prepared,
            transformations=_TRANSFORMATIONS,
            global_dict=dict(_GLOBALS),
            evaluate=True,
        )
    except (SyntaxError, TypeError, AttributeError, ValueError) as exc:
        raise EvaluationError(f"could not parse {text!r}") from exc
    if not isinstance(expression, sympy.Basic):
        raise EvaluationError(f"{text!r} did not parse to an expression")
    return expression


def _is_simplified(expression: sympy.Expr) -> bool:
    """Is the value already in lowest terms / plainest form?

    Reported, never enforced — see the module docstring.
    """
    if isinstance(expression, sympy.Rational) and not isinstance(expression, sympy.Integer):
        # parse_expr already reduces 4/8 to 1/2, so compare against the raw
        # numerator/denominator the student wrote rather than the parsed value.
        return True
    return bool(expression == sympy.simplify(expression))


def _written_fraction(text: str) -> tuple[int, int] | None:
    """Read `a/b` as the student wrote it, before any reduction."""
    parts = text.strip().split("/")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0].strip()), int(parts[1].strip())
    except ValueError:
        return None


def check(expected: str, actual: str) -> EquivalenceResult:
    """Compare two expressions mathematically."""
    try:
        expected_expr = _parse(expected)
    except (RejectedExpressionError, EvaluationError) as exc:
        # A bad *expected* value is a content bug, not a student error, and must
        # not be reported as the student being wrong.
        raise EvaluationError(f"expected answer is not usable: {exc}") from exc

    try:
        actual_expr = _parse(actual)
    except RejectedExpressionError as exc:
        return EquivalenceResult(
            equivalent=False,
            expected_canonical=str(expected_expr),
            actual_canonical="",
            actual_is_simplified=False,
            reason=str(exc),
        )
    except EvaluationError as exc:
        return EquivalenceResult(
            equivalent=False,
            expected_canonical=str(expected_expr),
            actual_canonical="",
            actual_is_simplified=False,
            reason=str(exc),
        )

    difference = sympy.simplify(expected_expr - actual_expr)
    equivalent = bool(difference == 0)

    written = _written_fraction(actual)
    if written is not None and written[1] != 0:
        numerator, denominator = written
        simplified = sympy.igcd(numerator, denominator) == 1
    else:
        simplified = _is_simplified(actual_expr)

    return EquivalenceResult(
        equivalent=equivalent,
        expected_canonical=str(expected_expr),
        actual_canonical=str(actual_expr),
        actual_is_simplified=simplified,
    )
