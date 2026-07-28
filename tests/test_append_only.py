"""M0.2 — the append-only guarantee (§5) is enforced by the session, not by
convention.

Both mutation routes are covered. Testing only the ORM path would leave
``session.execute(update(...))`` wide open, which is exactly the call someone
reaches for when writing a backfill script.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import delete, update
from sqlalchemy.orm import Session as DbSession

from packages.domain import tables as t
from packages.domain.append_only import AppendOnly, AppendOnlyError
from packages.domain.enums import HintSource, PipelineStage, ReviewReason, SessionState

NOW = dt.datetime(2026, 7, 27, 12, 0, 0, tzinfo=dt.UTC)

APPEND_ONLY_TABLES = [
    t.AttemptRow,
    t.LLMCallRow,
    t.DiagnosisLogRow,
    t.HintLogRow,
    t.GradeResultRow,
    t.ReviewVerdictRow,
]


def _hint_log() -> t.HintLogRow:
    return t.HintLogRow(
        id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        attempt_number=1,
        misconception_tag_id=None,
        curriculum_node_id=None,
        hint_text="How many counters do you have altogether?",
        hint_level=1,
        source=HintSource.GENERATED,
        leak_check_passed=True,
        leak_checker_version="leak/v1.2",
        llm_call_id=None,
    )


def test_append_and_read_is_allowed(session: DbSession) -> None:
    session.add(_hint_log())
    session.commit()
    assert session.query(t.HintLogRow).count() == 1


def test_orm_update_is_blocked(session: DbSession) -> None:
    row = _hint_log()
    session.add(row)
    session.commit()

    row.hint_text = "The answer is 12."
    with pytest.raises(AppendOnlyError, match="cannot be modified"):
        session.commit()


def test_orm_delete_is_blocked(session: DbSession) -> None:
    row = _hint_log()
    session.add(row)
    session.commit()

    session.delete(row)
    with pytest.raises(AppendOnlyError, match="cannot be deleted"):
        session.commit()


def test_bulk_update_is_blocked(session: DbSession) -> None:
    session.add(_hint_log())
    session.commit()

    with pytest.raises(AppendOnlyError, match="bulk-updated"):
        session.execute(update(t.HintLogRow).values(hint_text="The answer is 12."))


def test_bulk_delete_is_blocked(session: DbSession) -> None:
    session.add(_hint_log())
    session.commit()

    with pytest.raises(AppendOnlyError, match="bulk-deleted"):
        session.execute(delete(t.HintLogRow))


@pytest.mark.parametrize("row_cls", APPEND_ONLY_TABLES, ids=lambda c: c.__tablename__)
def test_every_log_table_is_marked_append_only(row_cls: type[t.Base]) -> None:
    """§5 names Attempt, HintLog, GradeResult and ReviewItem as append-only.

    The ledger (LLMCall), the diagnosis log, and the verdict trail carry the same
    audit weight, so they are covered too.
    """
    assert issubclass(row_cls, AppendOnly)


class TestMutableTablesStillWork:
    """The guarantee is scoped. Workflow state and queue state must stay mutable,
    or the state machine (§4) and the review queue (§3.6) cannot function."""

    def test_session_state_can_advance(self, session: DbSession) -> None:
        row = t.SessionRow(
            id=uuid.uuid4(),
            student_id=uuid.uuid4(),
            problem_id=uuid.uuid4(),
            started_at=NOW,
            state=SessionState.AWAITING_ANSWER,
            attempt_count=0,
        )
        session.add(row)
        session.commit()

        row.state = SessionState.DIAGNOSING
        row.attempt_count = 1
        session.commit()

        assert session.query(t.SessionRow).one().state is SessionState.DIAGNOSING

    def test_review_item_can_be_resolved(self, session: DbSession) -> None:
        """Resolving a queue item is a state change; the durable record of *what
        the teacher decided* is the appended ReviewVerdictRow."""
        item = t.ReviewItemRow(
            id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            reason=ReviewReason.LOW_CONFIDENCE,
            created_at=NOW,
            resolved_at=None,
        )
        session.add(item)
        session.commit()

        item.resolved_at = NOW + dt.timedelta(hours=1)
        session.commit()

        assert session.query(t.ReviewItemRow).one().resolved_at is not None


def test_ledger_row_cannot_be_rewritten(session: DbSession) -> None:
    """The §12 mitigation for nondeterminism only holds if the ledger is immutable
    — a rewritable audit trail defends nothing."""
    call = t.LLMCallRow(
        id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        stage=PipelineStage.DIAGNOSE,
        model_id="claude-haiku-4-5-20251001",
        prompt_version="diagnose/k-1/v3",
        input_payload={"student_answer": "2"},
        output_payload={"misconception_tag": "subtracted_instead_of_added"},
        tokens_in=412,
        tokens_out=38,
        latency_ms=630,
        cost_usd=0.00021,
        created_at=NOW,
    )
    session.add(call)
    session.commit()

    call.output_payload = {"misconception_tag": "something_else"}
    with pytest.raises(AppendOnlyError):
        session.commit()
