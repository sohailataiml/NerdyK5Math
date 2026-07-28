"""Where a §7 welfare signal goes (P1.8).

The screen is the easy half. The half that decides whether any of this protects a
child is what happens in the thirty seconds after it fires, and that is a person,
not a function — P1.8 requires "a defined on-call response", which no code can
supply.

What code *can* do is refuse to pretend. The central rule here:

    **A safety screen whose alerts route nowhere is worse than no screen.**

No screen is a known gap that someone is accountable for closing. A screen wired
to nothing produces green dashboards, a line in a district data-flow document,
and the belief that children are being watched over — while every alert lands in
a table nobody reads. That is a worse position than the honest gap, so it is made
unconstructable: `DatabaseAlertSink` cannot be built without a responder, and
`PipelineDeps` refuses to compose screening without a sink.

Delivery failure is treated the same way: it is recorded as a pipeline event, not
swallowed, because "the alert was raised" and "an adult was told" are different
facts and only one of them helps.
"""

from __future__ import annotations

import datetime as dt
from typing import Protocol
from uuid import UUID

from sqlalchemy.orm import Session as DbSession

from packages.domain.enums import URGENT_SAFETY_CATEGORIES, SafetyCategory
from packages.domain.mapping import to_row
from packages.domain.models import SafetyAlert
from packages.domain.tables import SafetyAlertRow


class AlertDeliveryError(RuntimeError):
    """The alert was recorded but no adult could be reached."""


class SafetyResponder(Protocol):
    """The on-call destination.

    A Protocol rather than a concrete integration because the real destination is
    site-specific — a counsellor's pager in one district, a safeguarding inbox in
    another — and hard-coding one would make the wrong thing easy to deploy.
    """

    def notify(self, alert: SafetyAlert, *, urgent: bool) -> None:
        """Put this in front of a human. Raise `AlertDeliveryError` if it cannot
        be done; do not return normally on failure."""
        ...


class ConsoleResponder:
    """Prints the alert. **For local development only.**

    Deliberately loud and deliberately unsuitable for production: a console line
    on a server nobody is tailing is the routed-nowhere failure wearing a
    different hat. It exists so the pipeline can be exercised end to end without
    tempting anyone to make the unconfigured case silently valid.
    """

    def __init__(self) -> None:
        self.delivered: list[SafetyAlert] = []

    def notify(self, alert: SafetyAlert, *, urgent: bool) -> None:
        marker = "URGENT" if urgent else "review"
        print(
            f"\n  !! SAFETY ALERT [{marker}] {alert.category.value}"
            f"\n     student {alert.student_id} · session {alert.session_id}"
            f"\n     wrote: {alert.excerpt!r}"
            f"\n     (ConsoleResponder is a development stand-in — this is not an "
            f"on-call path)\n"
        )
        self.delivered.append(alert)


class DatabaseAlertSink:
    """Persists the alert, then hands it to the responder.

    Order matters. Persisting first means a delivery failure leaves durable
    evidence that the signal existed; notifying first would risk a child's words
    reaching a pager and then vanishing.
    """

    def __init__(self, db: DbSession, responder: SafetyResponder) -> None:
        self._db = db
        self._responder = responder

    def raise_alert(
        self,
        *,
        session_id: UUID,
        student_id: UUID,
        attempt_number: int,
        category: SafetyCategory,
        excerpt: str,
        screener_version: str,
        classifier_ran: bool,
    ) -> SafetyAlert:
        alert = SafetyAlert(
            session_id=session_id,
            student_id=student_id,
            attempt_number=attempt_number,
            category=category,
            excerpt=excerpt,
            screener_version=screener_version,
            classifier_ran=classifier_ran,
            detected_at=dt.datetime.now(dt.UTC),
        )
        self._db.add(to_row(alert, SafetyAlertRow))
        self._db.flush()
        self._responder.notify(alert, urgent=category in URGENT_SAFETY_CATEGORIES)
        return alert
