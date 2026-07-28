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


PROBLEM_NAMESPACE = uuid.UUID("44444444-4444-4444-8444-444444444444")
"""Problem ids are derived from the prompt so re-seeding is genuinely
idempotent — a second run adds what is missing instead of duplicating what is
there, and the same problem has the same id in every environment, which is what
lets a session recorded locally be read against a deployed database."""

# Addition and subtraction within 20, which is the whole span the deterministic
# rule pre-check can diagnose (packages/fallbacks/rules.py) and the whole span
# the template library has hints for. A multiplication problem here would parse,
# fail every rule, diagnose as `unknown`, and serve a generic hint — a bigger
# problem set that makes the tutor look worse.
#
# Both §11.2 representations are exercised: addition with both parts within ten
# gets a ten-frame, everything else gets a number line.
_ADDITION: tuple[tuple[int, int], ...] = ((7, 5), (8, 6), (9, 4), (6, 7), (5, 9), (8, 3))
_SUBTRACTION: tuple[tuple[int, int], ...] = ((13, 8), (15, 7), (12, 9), (16, 8), (14, 6), (11, 4))


def problems() -> list[m.Problem]:
    """The fixture problem set — engineer-written, like everything else here.

    Twelve rather than two because a child, or anyone being shown this, meets
    the same question repeatedly otherwise: `start_session` picks uniformly at
    random with no memory of what came before, so two problems means half the
    sessions repeat the last one. That is a property of the fixture data, not of
    the sequencing, and P3.4's adaptive sequencer is where real ordering lands.
    """
    built: list[m.Problem] = []
    for node, operator, pairs in (
        (NODE_ADD_WITHIN_20, "+", _ADDITION),
        (NODE_SUB_WITHIN_20, "-", _SUBTRACTION),
    ):
        for left, right in pairs:
            prompt = f"What is {left} {operator} {right}?"
            answer = left + right if operator == "+" else left - right
            built.append(
                m.Problem(
                    id=uuid.uuid5(PROBLEM_NAMESPACE, prompt),
                    curriculum_node_id=node,
                    prompt=prompt,
                    correct_answer=str(answer),
                    answer_type=AnswerType.NUMERIC,
                    grade_band=GradeBand.K_1,
                )
            )
    return built


def seed(db: DbSession) -> None:
    """Insert the fixture KB. Idempotent, and additive on a second run.

    It previously returned early whenever any curriculum node existed, which
    made it idempotent but also inert: adding a problem to this file could never
    reach an already-seeded database, including a deployed one. Every row here
    has a stable id, so inserting only what is missing gives the same protection
    against duplicates and lets the fixture set grow.
    """
    inserted = 0
    for node in curriculum_nodes():
        if db.get(t.CurriculumNodeRow, node.id) is None:
            db.add(to_row(node, t.CurriculumNodeRow))
            inserted += 1
    for tag in misconception_tags():
        if db.get(t.MisconceptionTagRow, tag.id) is None:
            db.add(to_row(tag, t.MisconceptionTagRow))
            inserted += 1
    for problem in problems():
        if db.get(t.ProblemRow, problem.id) is None:
            db.add(to_row(problem, t.ProblemRow))
            inserted += 1
    if inserted:
        db.commit()
