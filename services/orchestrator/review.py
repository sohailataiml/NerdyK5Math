"""Routing a session to a teacher (§3.6, P0.8, P1.6).

The gap this closes: `ReviewItem` had a table, a queue endpoint, and a console
that renders it — and nothing in the system ever wrote one. Sessions escalated,
the child was told *"your teacher is going to look at this one with you"*, and
the queue stayed empty. A promise made to a child by a system that had no
mechanism to keep it.

Two decisions worth stating.

**One row per session, carrying the most significant reason.** `ReviewItem` has a
single `reason`, and a session that hit three routing conditions is still one
piece of work for one teacher. Splitting it into three rows would triple a queue
that P1.6 later has to keep under 25% of sessions, and a teacher clearing the
same session three times learns to clear things without reading them. The
precedence below decides which reason is shown; the session's event log holds the
rest, and that is what a teacher opens when they want the detail.

**Welfare alerts do not come through here.** `ReviewReason.SAFETY_FLAG` exists in
the enum and is deliberately never produced by this module. §7's distress path
has its own table, its own reader, and its own urgency — see
`services/safety/alerts.py`. Routing a possible disclosure into the queue a
teacher works through at the end of a lesson is how it gets read at the end of a
lesson. The enum member is left in place rather than removed because it appears
in the `review_item` DDL, but nothing should start emitting it.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from packages.domain.enums import UNKNOWN_TAG_LABEL, ReviewReason
from packages.domain.mapping import to_row
from packages.domain.models import ReviewItem
from packages.domain.tables import ReviewItemRow

LOW_CONFIDENCE_THRESHOLD = 0.8
"""§3.1's stated confidence bar. Below it the diagnosis is not trusted enough to
act on alone, which is exactly the case a teacher should see."""

PRECEDENCE: tuple[ReviewReason, ...] = (
    # A hint that could not be cleared, or a template forced after leak failures:
    # the guardrail fired, and what it caught is the highest-value thing a
    # teacher can look at during a pilot (it seeds the P0.5 corpus).
    ReviewReason.LEAK_FALLBACK,
    # The grader and the symbolic checker disagreed, or a "correct" arrived right
    # after a diagnosed gap. Either way the verdict is doubted by the system that
    # produced it.
    ReviewReason.SYMBOLIC_DISAGREEMENT,
    # The child used every hint level and still did not get there.
    ReviewReason.MAX_HINTS,
    # No misconception was identified, so the hint was generic — a teacher can
    # name what the model could not.
    ReviewReason.UNKNOWN_TAG,
    ReviewReason.LOW_CONFIDENCE,
    # Nothing specific fired. In Phase 0 this is every remaining session, because
    # P0.8 is 100% review; from Phase 1 it is P1.6's 2% sample.
    ReviewReason.AUDIT_SAMPLE,
)


class ReviewSink(Protocol):
    """Where a routed session goes."""

    def route(self, *, session_id: uuid.UUID, reason: ReviewReason) -> object: ...


def reasons_for_review(
    *,
    tag: str | None,
    confidence: float | None,
    leak_rejections: int,
    escalated: bool,
    hints_exhausted: bool,
    needs_review: bool,
    review_everything: bool,
) -> tuple[ReviewReason, ...]:
    """Which §3.6 conditions this session met, most significant first.

    A pure function of what happened, so the policy can be read and tested
    without a database, a model, or a session. P1.6 makes the thresholds
    runtime-configurable and tunes them with teachers; this is the shape that
    holds while Phase 0 reviews everything.
    """
    found: set[ReviewReason] = set()

    # `leak_rejections` is counted, not inferred from the hint source, and the
    # difference matters twice over. A template served in shadow mode is the
    # design — every Phase 0 session gets one. A template served during a
    # provider outage is degradation. Neither is the guardrail firing, and
    # treating them as such tags the entire pilot `leak_fallback`, burying the
    # sessions where the checker actually caught something inside the ones where
    # nothing went wrong. Those caught sessions are what seeds the P0.5 corpus,
    # so they are the last thing that should be hard to find.
    if leak_rejections > 0:
        found.add(ReviewReason.LEAK_FALLBACK)
    if needs_review:
        found.add(ReviewReason.SYMBOLIC_DISAGREEMENT)
    if hints_exhausted or escalated:
        # An escalation with no rejection is a *content* gap — no template
        # existed for this tag and level — not the guardrail firing. Both leave
        # the child without further help, which is what this reason means to the
        # teacher reading it, and neither should be labelled a leak event when
        # the checker never objected to anything.
        found.add(ReviewReason.MAX_HINTS)
    if tag == UNKNOWN_TAG_LABEL:
        found.add(ReviewReason.UNKNOWN_TAG)
    if confidence is not None and 0.0 < confidence < LOW_CONFIDENCE_THRESHOLD:
        found.add(ReviewReason.LOW_CONFIDENCE)

    if review_everything and not found:
        # P0.8: 100% review. A session where nothing went wrong is still a
        # session a teacher has not seen, and Phase 0's exit gates are counted in
        # teacher-reviewed sessions — so "nothing to flag" cannot mean "no row".
        found.add(ReviewReason.AUDIT_SAMPLE)

    return tuple(reason for reason in PRECEDENCE if reason in found)


class DatabaseReviewSink:
    """Writes the queue row, at most one open per session.

    `ReviewItem` is mutable state rather than a log — it is the queue's cursor,
    and `ReviewVerdict` is the durable evidence. So this is idempotent by check
    rather than by append: a session routed twice must not appear twice, or a
    teacher clears the same work repeatedly and learns to clear without reading.
    """

    def __init__(self, db: DbSession) -> None:
        self._db = db

    def route(self, *, session_id: uuid.UUID, reason: ReviewReason) -> ReviewItem | None:
        existing = self._db.execute(
            select(ReviewItemRow).where(
                ReviewItemRow.session_id == session_id,
                ReviewItemRow.resolved_at.is_(None),
            )
        ).scalar_one_or_none()
        if existing is not None:
            return None

        item = ReviewItem(
            session_id=session_id,
            reason=reason,
            created_at=dt.datetime.now(dt.UTC),
        )
        self._db.add(to_row(item, ReviewItemRow))
        self._db.flush()
        return item
