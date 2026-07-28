"""Fixture curriculum KB (M0.3).

Three nodes covering K–2 addition/subtraction within 20 — the strand
Implementation-Plan.md §7 recommends for Phase 0. Enough to exercise the
pipeline shape end-to-end; nowhere near the ~10 nodes Phase 0 needs, and
authored here by an engineer rather than a teacher, which the real KB never is
(§3.2: curriculum content is authored and approved by teachers).

Embeddings are left `None`. The embedding pipeline needs a model that has not
been chosen yet (§7 open question), so these nodes are exact-lookup-retrievable
but not similarity-retrievable until that lands.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy.orm import Session as DbSession

from packages.domain import models as m
from packages.domain import tables as t
from packages.domain.enums import AnswerType, GradeBand, Operation
from packages.domain.mapping import to_row

# Stable IDs so re-seeding is idempotent and fixtures can reference them.
NODE_ADD_WITHIN_20 = uuid.UUID("11111111-1111-4111-8111-111111111111")
NODE_SUB_WITHIN_20 = uuid.UUID("22222222-2222-4222-8222-222222222222")
NODE_MAKE_A_TEN = uuid.UUID("33333333-3333-4333-8333-333333333333")

TAG_SUBTRACTED_INSTEAD = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
TAG_ADDED_INSTEAD = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
TAG_COUNTED_START_WRONG = uuid.UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")

EPOCH = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


def curriculum_nodes() -> list[m.CurriculumNode]:
    return [
        m.CurriculumNode(
            id=NODE_MAKE_A_TEN,
            standard_code="1.OA.C.6",
            grade_band=GradeBand.K_1,
            definition="Add within 20 using the make-a-ten strategy.",
            remediation_strategies=[
                "Ten-frame: fill the frame to 10 first, then add what is left over.",
                "Number line: jump to 10, then jump the remainder.",
            ],
        ),
        m.CurriculumNode(
            id=NODE_ADD_WITHIN_20,
            standard_code="1.OA.A.1",
            grade_band=GradeBand.K_1,
            definition="Solve addition word problems within 20.",
            remediation_strategies=[
                "Counters: build both groups, then count the whole.",
                "Number line: start at the larger addend and count on.",
            ],
            prerequisite_ids=[NODE_MAKE_A_TEN],
        ),
        m.CurriculumNode(
            id=NODE_SUB_WITHIN_20,
            standard_code="1.OA.A.1s",
            grade_band=GradeBand.K_1,
            definition="Solve subtraction word problems within 20.",
            remediation_strategies=[
                "Counters: build the whole, then take the part away.",
                "Number line: start at the whole and count back.",
            ],
            prerequisite_ids=[NODE_ADD_WITHIN_20],
        ),
    ]


def misconception_tags() -> list[m.MisconceptionTag]:
    """Error patterns, never student traits (§7 bias review)."""
    return [
        m.MisconceptionTag(
            id=TAG_SUBTRACTED_INSTEAD,
            label="subtracted_instead_of_added",
            operation_type=Operation.ADDITION,
            description="Applied subtraction to an addition problem.",
            example_pattern="wrong_answer == abs(a - b)",
        ),
        m.MisconceptionTag(
            id=TAG_ADDED_INSTEAD,
            label="added_instead_of_subtracted",
            operation_type=Operation.SUBTRACTION,
            description="Applied addition to a subtraction problem.",
            example_pattern="wrong_answer == a + b",
        ),
        m.MisconceptionTag(
            id=TAG_COUNTED_START_WRONG,
            label="counted_on_from_wrong_start",
            operation_type=Operation.ADDITION,
            description="Counted on but included the starting number, landing one over.",
            example_pattern="wrong_answer == a + b + 1",
        ),
    ]


def problems() -> list[m.Problem]:
    return [
        m.Problem(
            curriculum_node_id=NODE_ADD_WITHIN_20,
            prompt="What is 7 + 5?",
            correct_answer="12",
            answer_type=AnswerType.NUMERIC,
            grade_band=GradeBand.K_1,
        ),
        m.Problem(
            curriculum_node_id=NODE_SUB_WITHIN_20,
            prompt="What is 13 - 8?",
            correct_answer="5",
            answer_type=AnswerType.NUMERIC,
            grade_band=GradeBand.K_1,
        ),
    ]


def seed(db: DbSession) -> None:
    """Insert the fixture KB. Idempotent — safe to run against a seeded database."""
    if db.query(t.CurriculumNodeRow).count():
        return
    for node in curriculum_nodes():
        db.add(to_row(node, t.CurriculumNodeRow))
    for tag in misconception_tags():
        db.add(to_row(tag, t.MisconceptionTagRow))
    for problem in problems():
        db.add(to_row(problem, t.ProblemRow))
    db.commit()
