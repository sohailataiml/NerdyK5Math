"""Roles, tenancy, and authorization (M0.9).

Default deny, and scope derived from data rather than granted by a role. See
``policy`` for the reasoning.
"""

from packages.auth.policy import (
    AuthorizationError,
    Scope,
    can_author_curriculum,
    can_control_rollout,
    can_publish_prompts,
    can_read_audit_trail,
    can_read_review_queue,
    can_read_session,
    can_read_student,
    can_resolve_review_item,
    require,
)
from packages.auth.scope import resolve_scope

__all__ = [
    "AuthorizationError",
    "Scope",
    "can_author_curriculum",
    "can_control_rollout",
    "can_publish_prompts",
    "can_read_audit_trail",
    "can_read_review_queue",
    "can_read_session",
    "can_read_student",
    "can_resolve_review_item",
    "require",
    "resolve_scope",
]
