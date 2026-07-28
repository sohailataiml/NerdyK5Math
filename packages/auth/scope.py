"""Resolving a principal's tenancy from the database (M0.9).

Kept apart from `policy` on purpose: the policy is pure and exhaustively
testable, and this is the one place that decides *which* students a principal can
reach. A bug here silently widens every rule at once, so it is small enough to
read in full.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from packages.auth.policy import Scope
from packages.domain.enums import Role
from packages.domain.models import Principal
from packages.domain.tables import ClassroomRow, EnrollmentRow


def resolve_scope(db: DbSession, principal: Principal) -> Scope:
    """Compute the set of students this principal may see."""
    if principal.role is Role.ADMIN:
        # Admins are unscoped, so the set is left empty rather than materialising
        # every student in the system — the policy short-circuits on the role.
        return Scope(principal=principal)

    if principal.role is Role.STUDENT:
        assert principal.student_id is not None  # enforced by the entity validator
        return Scope(principal=principal, student_ids=frozenset({principal.student_id}))

    student_ids = (
        db.execute(
            select(EnrollmentRow.student_id)
            .join(ClassroomRow, ClassroomRow.id == EnrollmentRow.classroom_id)
            .where(ClassroomRow.teacher_id == principal.id)
        )
        .scalars()
        .all()
    )
    return Scope(principal=principal, student_ids=frozenset(student_ids))
