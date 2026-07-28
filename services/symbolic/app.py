"""The symbolic service HTTP surface (M0.7).

Isolation is the point of running this as a separate service rather than a
library call. It is the one component that evaluates attacker-influenced input,
so it gets its own process with no network egress, no filesystem writes, and
capped CPU and memory (see `ops/docker-compose.yml`). A compromise here should
reach nothing.

The API reports mathematical facts only. Whether an unsimplified answer counts
as correct is grade-band policy and belongs to the grading stage (§3.5).
"""

from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field

from services.symbolic.equivalence import EvaluationError, check
from services.symbolic.parsing import MAX_LENGTH

app = FastAPI(
    title="Symbolic equivalence",
    description="Mathematical equivalence checking for closed-form answers (Architecture.md §3.5)",
    version="0.1.0",
)


class EquivalenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected: str = Field(min_length=1, max_length=MAX_LENGTH)
    actual: str = Field(min_length=1, max_length=MAX_LENGTH)


class EquivalenceResponse(BaseModel):
    equivalent: bool
    expected_canonical: str
    actual_canonical: str
    actual_is_simplified: bool
    reason: str | None = None


class ErrorResponse(BaseModel):
    detail: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post(
    "/equivalent",
    response_model=EquivalenceResponse,
    responses={422: {"model": ErrorResponse}},
)
def equivalent(request: EquivalenceRequest) -> EquivalenceResponse:
    """Compare a student's answer to the expected one.

    A malformed *student* answer is a 200 with `equivalent: false` and a reason —
    it is a normal outcome of a child typing something odd, not a server error.
    A malformed *expected* answer is a 422: that is a content bug, and silently
    grading it as a student mistake would blame the child for it.
    """
    try:
        result = check(request.expected, request.actual)
    except EvaluationError as exc:
        from fastapi import HTTPException

        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return EquivalenceResponse(
        equivalent=result.equivalent,
        expected_canonical=result.expected_canonical,
        actual_canonical=result.actual_canonical,
        actual_is_simplified=result.actual_is_simplified,
        reason=result.reason,
    )
