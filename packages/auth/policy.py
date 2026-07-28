"""Authorization (M0.9).

This module decides who may read a child's work. Two properties shape the whole
design:

**Default deny.** Every decision starts at "no" and requires a rule to say
otherwise. The alternative — listing what is forbidden — fails open the moment
someone adds a resource and forgets the check, and failing open here means a
teacher reading another school's students.

**Scope is data, not a role.** A teacher's role grants nothing on its own; what
they may read is derived from the classrooms they own and the students enrolled
in them. So "is this a teacher?" is never the whole question, and a check written
as `if role is TEACHER` cannot pass review — it would grant every teacher access
to every child in the system.

The functions here take a `Scope` — the caller's resolved tenancy — rather than a
database session, so the policy is pure and its negatives are exhaustively
testable without fixtures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from packages.domain.enums import Role
from packages.domain.models import Principal


class AuthorizationError(PermissionError):
    """Raised when a principal is refused. Carries no detail about what exists.

    Deliberately uniform: a message distinguishing "no such session" from "not
    your session" tells an attacker which session IDs are real.
    """


@dataclass(frozen=True)
class Scope:
    """A principal plus the tenancy resolved for them.

    `student_ids` is the complete set of students this principal may see. For a
    teacher it is everyone enrolled in their classrooms; for a student it is
    themselves; for an admin it is unused, because admins are not scoped.
    """

    principal: Principal
    student_ids: frozenset[UUID] = field(default_factory=frozenset)

    @property
    def role(self) -> Role:
        return self.principal.role

    @property
    def is_admin(self) -> bool:
        return self.principal.role is Role.ADMIN


def _may_see_student(scope: Scope, student_id: UUID) -> bool:
    if scope.is_admin:
        return True
    return student_id in scope.student_ids


def can_read_student(scope: Scope, student_id: UUID) -> bool:
    return _may_see_student(scope, student_id)


def can_read_session(scope: Scope, *, student_id: UUID) -> bool:
    """A session belongs to a student; visibility follows that student."""
    return _may_see_student(scope, student_id)


def can_read_review_queue(scope: Scope) -> bool:
    """§3.6's queue is a teacher surface. Students never see it — a child should
    not learn that their work was flagged for review by discovering the queue."""
    return scope.role in (Role.TEACHER, Role.ADMIN)


def can_resolve_review_item(scope: Scope, *, student_id: UUID) -> bool:
    """Resolving is a teacher action, and only for their own students."""
    if scope.role not in (Role.TEACHER, Role.ADMIN):
        return False
    return _may_see_student(scope, student_id)


def can_read_audit_trail(scope: Scope, *, student_id: UUID) -> bool:
    """§6 marks `/admin/llm-calls/{session_id}` teacher/admin only.

    A teacher may read it — that is how a grade gets defended — but only for a
    session belonging to one of their own students. The audit trail contains the
    child's actual answers.
    """
    if scope.role not in (Role.TEACHER, Role.ADMIN):
        return False
    return _may_see_student(scope, student_id)


def can_author_curriculum(scope: Scope) -> bool:
    """§3.2: curriculum is authored and approved by teachers and designers."""
    return scope.role in (Role.TEACHER, Role.ADMIN)


def can_publish_prompts(scope: Scope) -> bool:
    """Prompt versions are system configuration, not curriculum content.

    A published prompt governs what every child in the system is told, and §8's
    metrics are segmented by it — that is an operator decision, not a classroom
    one.
    """
    return scope.is_admin


def can_control_rollout(scope: Scope) -> bool:
    """P1.1's staged rollout and kill switch are operator controls, not classroom ones.

    Same reasoning as `can_publish_prompts`, and it lands harder here: this
    setting decides whether generated text reaches children at all. A teacher
    turning it *off* for their own class would be reasonable and is not what this
    control does — there is one setting for the whole deployment, so anyone who
    can change it changes it for every child. Scoping that to a role which is
    granted per classroom would be granting a system-wide switch to whoever has
    the most students.
    """
    return scope.is_admin


def require(allowed: bool, action: str) -> None:
    """Convert a policy answer into an enforced one.

    Callers that forget this get a boolean they can ignore; the shape of the API
    is meant to make the enforcement obvious at the call site.
    """
    if not allowed:
        raise AuthorizationError(f"not permitted: {action}")
