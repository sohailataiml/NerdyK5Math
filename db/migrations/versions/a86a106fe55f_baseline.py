"""baseline — all §5 entities

Revision ID: a86a106fe55f
Revises:
Create Date: 2026-07-27 18:32:20.237082

Runs on both Postgres and SQLite. The only dialect-specific pieces are the
pgvector extension and the `curriculum_node.embedding` column type, which is a
`vector` under Postgres and plain JSON under SQLite so the test suite needs no
container.
"""

from __future__ import annotations

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op

revision: str = "a86a106fe55f"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # pgvector must exist before curriculum_node's embedding column is created.
    # No-op on SQLite, which gets the JSON variant of that column instead.
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "curriculum_node",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("standard_code", sa.String(length=64), nullable=False),
        sa.Column(
            "grade_band",
            sa.Enum("K_1", "G2_3", "G4_5", "G6_8", "G9_12", name="grade_band", native_enum=False),
            nullable=False,
        ),
        sa.Column("definition", sa.Text(), nullable=False),
        sa.Column("remediation_strategies", sa.JSON(), nullable=False),
        sa.Column("prerequisite_ids", sa.JSON(), nullable=False),
        sa.Column("embedding_version", sa.Integer(), nullable=False),
        sa.Column(
            "embedding",
            pgvector.sqlalchemy.vector.VECTOR(dim=1024).with_variant(sa.JSON(), "sqlite"),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("curriculum_node", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_curriculum_node_standard_code"), ["standard_code"], unique=False
        )

    op.create_table(
        "misconception_tag",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column(
            "operation_type",
            sa.Enum(
                "ADDITION",
                "SUBTRACTION",
                "MULTIPLICATION",
                "DIVISION",
                "FRACTIONS",
                "ALGEBRA",
                name="operation",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("example_pattern", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("label"),
    )
    op.create_table(
        "student",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("grade_level", sa.Integer(), nullable=False),
        sa.Column("iep_flags", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("grade_level >= 0 AND grade_level <= 12", name="ck_grade"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "problem",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("curriculum_node_id", sa.Uuid(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("correct_answer", sa.Text(), nullable=False),
        sa.Column(
            "answer_type",
            sa.Enum(
                "NUMERIC",
                "FRACTION",
                "EXPRESSION",
                "MULTIPLE_CHOICE",
                "SHORT_RESPONSE",
                "FREE_TEXT",
                name="answer_type",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "grade_band",
            sa.Enum("K_1", "G2_3", "G4_5", "G6_8", "G9_12", name="grade_band", native_enum=False),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["curriculum_node_id"],
            ["curriculum_node.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "session",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("student_id", sa.Uuid(), nullable=False),
        sa.Column("problem_id", sa.Uuid(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "state",
            sa.Enum(
                "AWAITING_ANSWER",
                "DIAGNOSING",
                "RETRIEVING_CURRICULUM",
                "GENERATING_HINT",
                "LEAK_CHECKING",
                "AWAITING_STUDENT_RETRY",
                "GRADING",
                "COMPLETE",
                "ESCALATED",
                name="session_state",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["problem_id"],
            ["problem.id"],
        ),
        sa.ForeignKeyConstraint(
            ["student_id"],
            ["student.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("session", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_session_student_id"), ["student_id"], unique=False)

    op.create_table(
        "attempt",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("student_answer", sa.Text(), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hint_level_shown", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["session.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("attempt", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_attempt_session_id"), ["session_id"], unique=False)

    op.create_table(
        "llm_call",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column(
            "stage",
            sa.Enum(
                "DIAGNOSE",
                "RERANK",
                "GENERATE_HINT",
                "LEAK_CHECK",
                "GRADE",
                "SAFETY_SCREEN",
                "TEACHER_SUMMARY",
                name="pipeline_stage",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("model_id", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("input_payload", sa.JSON(), nullable=False),
        sa.Column("output_payload", sa.JSON(), nullable=False),
        sa.Column("tokens_in", sa.Integer(), nullable=False),
        sa.Column("tokens_out", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["session.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("llm_call", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_llm_call_prompt_version"), ["prompt_version"], unique=False
        )
        batch_op.create_index(batch_op.f("ix_llm_call_session_id"), ["session_id"], unique=False)
        batch_op.create_index(
            "ix_llm_call_stage_version", ["stage", "prompt_version", "created_at"], unique=False
        )

    op.create_table(
        "review_item",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column(
            "reason",
            sa.Enum(
                "LOW_CONFIDENCE",
                "RUBRIC_DISAGREEMENT",
                "SYMBOLIC_DISAGREEMENT",
                "UNKNOWN_TAG",
                "MAX_HINTS",
                "SAFETY_FLAG",
                "LEAK_FALLBACK",
                "AUDIT_SAMPLE",
                name="review_reason",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["session.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("review_item", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_review_item_session_id"), ["session_id"], unique=False)
        batch_op.create_index("ix_review_open", ["resolved_at", "created_at"], unique=False)

    op.create_table(
        "diagnosis_log",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("misconception_tag_id", sa.Uuid(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("alternatives", sa.JSON(), nullable=False),
        sa.Column(
            "source",
            sa.Enum("RULE", "LLM", name="diagnosis_source", native_enum=False),
            nullable=False,
        ),
        sa.Column("llm_call_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["attempt.id"],
        ),
        sa.ForeignKeyConstraint(
            ["llm_call_id"],
            ["llm_call.id"],
        ),
        sa.ForeignKeyConstraint(
            ["misconception_tag_id"],
            ["misconception_tag.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("diagnosis_log", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_diagnosis_log_attempt_id"), ["attempt_id"], unique=False
        )

    op.create_table(
        "grade_result",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column(
            "method",
            sa.Enum(
                "SYMBOLIC", "RUBRIC", "HYBRID", "TEACHER", name="grade_method", native_enum=False
            ),
            nullable=False,
        ),
        sa.Column("rubric_breakdown", sa.JSON(), nullable=False),
        sa.Column("symbolic_agreed", sa.Boolean(), nullable=True),
        sa.Column("llm_call_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["attempt.id"],
        ),
        sa.ForeignKeyConstraint(
            ["llm_call_id"],
            ["llm_call.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("grade_result", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_grade_result_attempt_id"), ["attempt_id"], unique=False
        )

    op.create_table(
        "hint_log",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("misconception_tag_id", sa.Uuid(), nullable=True),
        sa.Column("curriculum_node_id", sa.Uuid(), nullable=True),
        sa.Column("hint_text", sa.Text(), nullable=False),
        sa.Column("hint_level", sa.Integer(), nullable=False),
        sa.Column(
            "source",
            sa.Enum(
                "GENERATED", "CACHED", "TEMPLATE_FALLBACK", name="hint_source", native_enum=False
            ),
            nullable=False,
        ),
        sa.Column("leak_check_passed", sa.Boolean(), nullable=False),
        sa.Column("leak_checker_version", sa.String(length=64), nullable=False),
        sa.Column("llm_call_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["curriculum_node_id"],
            ["curriculum_node.id"],
        ),
        sa.ForeignKeyConstraint(
            ["llm_call_id"],
            ["llm_call.id"],
        ),
        sa.ForeignKeyConstraint(
            ["misconception_tag_id"],
            ["misconception_tag.id"],
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["session.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("hint_log", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_hint_log_session_id"), ["session_id"], unique=False)

    op.create_table(
        "review_verdict",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("review_item_id", sa.Uuid(), nullable=False),
        sa.Column("teacher_id", sa.Uuid(), nullable=False),
        sa.Column(
            "verdict",
            sa.Enum(
                "CONFIRMED",
                "OVERRIDDEN",
                "NEEDS_FOLLOWUP",
                name="teacher_verdict",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["review_item_id"],
            ["review_item.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("review_verdict", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_review_verdict_review_item_id"), ["review_item_id"], unique=False
        )

    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    with op.batch_alter_table("review_verdict", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_review_verdict_review_item_id"))

    op.drop_table("review_verdict")
    with op.batch_alter_table("hint_log", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_hint_log_session_id"))

    op.drop_table("hint_log")
    with op.batch_alter_table("grade_result", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_grade_result_attempt_id"))

    op.drop_table("grade_result")
    with op.batch_alter_table("diagnosis_log", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_diagnosis_log_attempt_id"))

    op.drop_table("diagnosis_log")
    with op.batch_alter_table("review_item", schema=None) as batch_op:
        batch_op.drop_index("ix_review_open")
        batch_op.drop_index(batch_op.f("ix_review_item_session_id"))

    op.drop_table("review_item")
    with op.batch_alter_table("llm_call", schema=None) as batch_op:
        batch_op.drop_index("ix_llm_call_stage_version")
        batch_op.drop_index(batch_op.f("ix_llm_call_session_id"))
        batch_op.drop_index(batch_op.f("ix_llm_call_prompt_version"))

    op.drop_table("llm_call")
    with op.batch_alter_table("attempt", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_attempt_session_id"))

    op.drop_table("attempt")
    with op.batch_alter_table("session", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_session_student_id"))

    op.drop_table("session")
    op.drop_table("problem")
    op.drop_table("student")
    op.drop_table("misconception_tag")
    with op.batch_alter_table("curriculum_node", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_curriculum_node_standard_code"))

    op.drop_table("curriculum_node")
    # ### end Alembic commands ###
