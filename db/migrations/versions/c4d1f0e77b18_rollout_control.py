"""rollout control (P1.1)

The Phase 1 staged rollout and its kill switch. One append-only table whose
latest row is the current setting, so a change takes effect on the next attempt
without a restart — a kill switch that needs a deploy is not a kill switch.

Append-only for the same reason as every other log-bearing table (§5): if
generation is turned off after a leak, the window during which it was on is
exactly what the incident review needs, and a mutable settings row would have
overwritten it.

Revision ID: c4d1f0e77b18
Revises: a922f431b2bd
Create Date: 2026-07-28 10:14:02.118447
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4d1f0e77b18"
down_revision: str | None = "a922f431b2bd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rollout_change",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("generation_enabled", sa.Boolean(), nullable=False),
        sa.Column("percentage", sa.Integer(), nullable=False),
        sa.Column("changed_by", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("percentage >= 0 AND percentage <= 100", name="ck_rollout_percentage"),
        sa.ForeignKeyConstraint(
            ["changed_by"],
            ["principal.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_rollout_change_created_at"), "rollout_change", ["created_at"], unique=False
    )
    op.create_index("ix_rollout_current", "rollout_change", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_rollout_current", table_name="rollout_change")
    op.drop_index(op.f("ix_rollout_change_created_at"), table_name="rollout_change")
    op.drop_table("rollout_change")
