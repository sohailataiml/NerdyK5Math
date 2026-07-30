"""Recording what the diagnoser concluded (§5's `DiagnosisLog`).

The third table in this repo to have an entity, a migration, and no producer.
`ReviewItem` was the first, `GradeResult` the second, and Findings.md said of the
second that "the pattern is worth checking for deliberately rather than waiting
to notice it twice". Checking deliberately found this one: every attempt ran the
diagnoser, and `diagnosis_log` was empty in every real session — the only code
that ever inserted a row was `scripts/demo_session.py`.

Why the event is not enough, which is the same argument `grades.py` makes and a
sharper one here. §8 names diagnoser accuracy and calibration against
teacher-confirmed tags as "the core quality metric for the whole system". That
metric is a join: diagnoses on one side, `ReviewVerdict` on the other. Recovering
the left side by scanning `pipeline_event` detail JSON works for one session and
does not work for a phase — it is unindexed, untyped, and the shape of the detail
dict is not a contract. Phase 0's exit gate depends on this measurement, so the
diagnosis needs to be a row.

**A label that is not in the taxonomy resolves to no tag, not to a new tag.**
§3.1 constrains the diagnoser's output vocabulary to the authored taxonomy, and a
sink that created rows for unrecognised labels would quietly let the model extend
it — the taxonomy is authored with a teacher (P0.1), and growing it is not a
runtime decision. The label is preserved in `evidence` so the signal is not lost.

Append-only, like every log-bearing table (§5). A re-diagnosis appends; the first
row is what the child's hint was actually chosen from.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from packages.domain.enums import UNKNOWN_TAG_LABEL, DiagnosisSource
from packages.domain.mapping import to_row
from packages.domain.models import DiagnosisLog
from packages.domain.tables import DiagnosisLogRow, MisconceptionTagRow


class DatabaseDiagnosisSink:
    def __init__(self, db: DbSession) -> None:
        self._db = db

    def record(
        self,
        *,
        attempt_id: UUID,
        tag: str,
        confidence: float,
        evidence: str,
        source: DiagnosisSource,
        llm_call_id: UUID | None = None,
    ) -> DiagnosisLog:
        tag_id = self._resolve(tag)
        if tag_id is None and tag != UNKNOWN_TAG_LABEL:
            # Not in the taxonomy. Recorded as an abstention so no downstream
            # count treats it as a diagnosis, with the label kept so the
            # out-of-vocabulary reply is still measurable.
            evidence = f"[unrecognised tag {tag!r}] {evidence}"
        log = DiagnosisLog(
            attempt_id=attempt_id,
            misconception_tag_id=tag_id,
            confidence=confidence,
            evidence=evidence,
            source=source,
            llm_call_id=llm_call_id,
        )
        self._db.add(to_row(log, DiagnosisLogRow))
        self._db.flush()
        return log

    def _resolve(self, tag: str) -> UUID | None:
        """Label to taxonomy id. `unknown` has no row, and is not meant to."""
        if tag == UNKNOWN_TAG_LABEL:
            return None
        return self._db.execute(
            select(MisconceptionTagRow.id).where(MisconceptionTagRow.label == tag)
        ).scalar_one_or_none()


class InMemoryDiagnosisSink:
    """For stage tests: assert on the diagnosis without a database."""

    def __init__(self) -> None:
        self.logs: list[dict[str, object]] = []

    def record(
        self,
        *,
        attempt_id: UUID,
        tag: str,
        confidence: float,
        evidence: str,
        source: DiagnosisSource,
        llm_call_id: UUID | None = None,
    ) -> None:
        self.logs.append(
            {
                "attempt_id": attempt_id,
                "tag": tag,
                "confidence": confidence,
                "evidence": evidence,
                "source": source,
                "llm_call_id": llm_call_id,
            }
        )
