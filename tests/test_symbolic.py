"""M0.7 — the symbolic service.

Two concerns, in order of importance:

1. **Hostile input cannot execute, hang, or exhaust memory.** This is the one
   component that evaluates attacker-influenced text, so the allowlist gets an
   adversarial suite rather than a couple of happy-path checks.
2. **Equivalence is mathematical, not textual** (§3.5) — and the service reports
   simplification as a *fact* without deciding whether it matters, because that
   is grade-band policy.
"""

from __future__ import annotations

import contextlib
import time

import pytest
from fastapi.testclient import TestClient

from services.symbolic.app import app
from services.symbolic.equivalence import EvaluationError, check
from services.symbolic.parsing import MAX_LENGTH, RejectedExpressionError, validate

client = TestClient(app)


class TestHostileInput:
    """Every one of these must be refused *before* SymPy sees it."""

    @pytest.mark.parametrize(
        "payload",
        [
            "__import__('os').system('id')",
            "os.system('id')",
            "eval('1+1')",
            "open('/etc/passwd').read()",
            "().__class__.__bases__[0].__subclasses__()",
            "lambda: 1",
            "exit()",
            "print(1)",
            "1; import os",
            "globals()",
        ],
    )
    def test_code_execution_attempts_are_rejected(self, payload: str) -> None:
        with pytest.raises(RejectedExpressionError):
            validate(payload)

    @pytest.mark.parametrize(
        "payload",
        [
            "9**9**9**9",  # exponent tower — 11 chars, unbounded memory
            "2^2^2^2",
            "9**999999",  # single huge exponent
            "1" * 60,  # 60-digit literal
        ],
    )
    def test_expression_bombs_are_rejected(self, payload: str) -> None:
        """The attack a character allowlist alone would let straight through."""
        with pytest.raises(RejectedExpressionError):
            validate(payload)

    def test_oversized_input_is_rejected(self) -> None:
        with pytest.raises(RejectedExpressionError, match="longer than"):
            validate("1+" * MAX_LENGTH)

    @pytest.mark.parametrize("payload", ["sin(1)", "pi", "E", "sqrt(4)", "factorial(20)"])
    def test_function_and_constant_names_are_rejected(self, payload: str) -> None:
        """Even harmless-looking names are name-based routes into the evaluator,
        and none of them belong in a K-12 arithmetic answer."""
        with pytest.raises(RejectedExpressionError, match="unsupported name"):
            validate(payload)

    def test_empty_input_is_rejected(self) -> None:
        with pytest.raises(RejectedExpressionError, match="empty"):
            validate("   ")

    def test_rejection_is_distinguishable_from_being_wrong(self) -> None:
        """A refused answer must not silently read as "not equivalent" — the two
        mean very different things for a child's grade."""
        result = check("12", "__import__('os')")
        assert result.equivalent is False
        assert result.reason is not None

    def test_hostile_corpus_completes_quickly(self) -> None:
        """The point of the limits: none of these may hang the service."""
        payloads = [
            "9**9**9**9",
            "2^2^2^2",
            "(" * 50 + "1" + ")" * 50,
            "1/" * 40 + "1",
            "9" * 40,
        ]
        started = time.perf_counter()
        for payload in payloads:
            with contextlib.suppress(EvaluationError):
                check("1", payload)
        assert time.perf_counter() - started < 5.0


class TestDefenceInDepth:
    """What each layer actually stops, verified rather than assumed.

    Measured by running the parser directly with validation bypassed:

    - Restricted globals DO stop name-based attacks. `factorial(20)`, `sin(1)`
      and `__import__('os')` all die on `NameError: name 'Function' is not
      defined`, because SymPy's generated code cannot resolve the constructor.
    - Restricted globals DO NOT stop expression bombs. `9**9**9` ran unbounded
      until it was killed manually. **The pre-parse validation is the only thing
      standing between a bored ten-year-old and an exhausted container**, which
      is why `MAX_POWER_OPERATORS` and `MAX_EXPONENT` must not be relaxed to
      accommodate some future expression type without a replacement guard.

    No bomb appears in these tests for the obvious reason.
    """

    def test_restricted_globals_block_name_resolution(self) -> None:
        from sympy.parsing.sympy_parser import parse_expr

        from services.symbolic.equivalence import _GLOBALS, _TRANSFORMATIONS

        for payload in ["factorial(20)", "sin(1)", "__import__('os')"]:
            with pytest.raises(NameError):
                parse_expr(payload, transformations=_TRANSFORMATIONS, global_dict=dict(_GLOBALS))

    def test_sympy_namespace_is_not_reachable(self) -> None:
        """The globals allowlist is exactly four constructors — not SymPy's
        namespace, which would put `sympify` one name away from student input."""
        from services.symbolic.equivalence import _GLOBALS

        assert set(_GLOBALS) == {"Integer", "Float", "Rational", "Symbol"}


class TestEquivalence:
    """§3.5: mathematical equivalence, not string matching."""

    @pytest.mark.parametrize(
        ("expected", "actual"),
        [
            ("1/2", "4/8"),  # the §3.5 worked example
            ("1/2", "0.5"),
            ("12", "12"),
            ("12", "6+6"),
            ("12", "12.0"),
            ("3/4", "6/8"),
            ("2*x+3", "3+2*x"),
            ("2*x+3", "x+x+3"),
            ("0", "5-5"),
        ],
    )
    def test_equivalent_forms(self, expected: str, actual: str) -> None:
        assert check(expected, actual).equivalent is True

    @pytest.mark.parametrize(
        ("expected", "actual"),
        [("12", "13"), ("1/2", "1/3"), ("2*x+3", "2*x+4"), ("12", "21")],
    )
    def test_non_equivalent_forms(self, expected: str, actual: str) -> None:
        assert check(expected, actual).equivalent is False

    def test_bad_expected_answer_is_an_error_not_a_wrong_grade(self) -> None:
        """A broken curriculum entry must not be charged to the student."""
        with pytest.raises(EvaluationError, match="expected answer"):
            check("__bad__", "12")


class TestSimplificationIsReportedNotEnforced:
    """§3.5 / P2.1: `4/8` is right in grade 3 and wrong in grade 6, and the CAS
    cannot know which — so it reports the fact and the grader applies policy."""

    def test_unsimplified_fraction_is_equivalent_but_flagged(self) -> None:
        result = check("1/2", "4/8")
        assert result.equivalent is True
        assert result.actual_is_simplified is False

    def test_simplified_fraction_is_flagged_as_such(self) -> None:
        result = check("1/2", "1/2")
        assert result.equivalent is True
        assert result.actual_is_simplified is True


class TestApi:
    def test_health(self) -> None:
        assert client.get("/health").json() == {"status": "ok"}

    def test_equivalent_endpoint(self) -> None:
        response = client.post("/equivalent", json={"expected": "1/2", "actual": "4/8"})
        assert response.status_code == 200
        body = response.json()
        assert body["equivalent"] is True
        assert body["actual_is_simplified"] is False

    def test_hostile_student_answer_is_a_normal_200(self) -> None:
        """A child typing something odd is not a server error."""
        response = client.post("/equivalent", json={"expected": "12", "actual": "__import__('os')"})
        assert response.status_code == 200
        assert response.json()["equivalent"] is False
        assert response.json()["reason"]

    def test_bad_expected_answer_is_422(self) -> None:
        response = client.post("/equivalent", json={"expected": "sin(x)", "actual": "12"})
        assert response.status_code == 422

    def test_unknown_field_is_rejected(self) -> None:
        response = client.post("/equivalent", json={"expected": "1", "actual": "1", "extra": "x"})
        assert response.status_code == 422
