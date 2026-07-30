"""The diagnosis leaves a durable, joinable record (§5, §8).

The third table in this repo with an entity, a migration, and no producer.
`ReviewItem` was the first, `GradeResult` the second, and `Findings.md` said of
the second that the pattern "is worth checking for deliberately rather than
waiting to notice it twice". This is what checking deliberately found: every
attempt ran the diagnoser, and `diagnosis_log` was empty in every real session.

Why the event was not enough is a sharper argument here than it was for grades.
§8 names diagnoser accuracy and calibration against teacher-confirmed tags as the
core quality metric for the whole system, and that metric is a join — diagnoses on
one side, `ReviewVerdict` on the other. Recovering the left side by scanning
`pipeline_event` detail JSON works for one session and not for a phase, because
the detail dict is unindexed, untyped, and not a contract. Phase 0's exit gate
depends on the measurement, so the two load-bearing tests here are
`test_an_abstention_is_recorded_too` (without it the table reports a diagnoser
that is always right about the things it has opinions on, and hides how rarely it
has one) and `test_an_unrecognised_label_does_not_become_a_tag` (without it the
model quietly extends a taxonomy that is authored with a teacher).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from packages.curriculum.seed import seed
from packages.domain import tables as t
from packages.domain.enums import UNKNOWN_TAG_LABEL, DiagnosisSource, GradeBand
from packages.llm import LLMClient
from packages.llm.fake import FakeTransport
from packages.llm.ledger import InMemoryLedger
from packages.prompts import PromptRegistry
from packages.telemetry import EventRecorder, InMemoryEventSink
from services.orchestrator import graph
from services.orchestrator.diagnoses import DatabaseDiagnosisSink, InMemoryDiagnosisSink
from services.orchestrator.state import PipelineDeps, Problem

PROBLEM = Problem(
    prompt="What is 7 + 5?",
    correct_answer="12",
    grade_band=GradeBand.K_1,
    operands={"a": "7", "b": "5"},
)

RULE_TAG = "subtracted_instead_of_added"
"""The rule pre-check's verdict on `2` for `7 + 5` — an exact identity (§3.1)."""

UNDIAGNOSABLE = "99"
"""Wrong, and not any of the three arithmetic identities `RULES` recognises.

The rule layer has to fall through for the LLM path to be reachable at all, and
`13` would not do it: that is `a + b + 1`, which `counted_on_from_wrong_start`
matches exactly.
"""


def _responder(tag: str, confidence: float) -> Callable[[str, str], str]:
    """Answer the diagnoser in its §3.1 JSON contract; anything else gets SAFE."""

    def respond(system: str, _user: str) -> str:
        if "gives away the answer" in system:
            return "SAFE"
        return f'{{"tag": "{tag}", "confidence": {confidence}, "evidence": "carried through"}}'

    return respond


def _deps(
    db: DbSession,
    diagnoses: InMemoryDiagnosisSink | None = None,
    *,
    llm: LLMClient | None = None,
) -> PipelineDeps:
    return PipelineDeps(
        recorder=EventRecorder(InMemoryEventSink(), uuid.uuid4()),
        prompts=PromptRegistry(),
        llm=llm,
        db=db,
        diagnosis_sink=diagnoses,
    )


class TestTheDiagnosisIsDurable:
    def test_a_rule_diagnosis_is_written_for_the_attempt(self, session: DbSession) -> None:
        """`diagnosis_log` was a table nothing outside a demo script wrote to —
        the same shape of gap as the review queue nobody filled."""
        seed(session)
        diagnoses = InMemoryDiagnosisSink()
        deps = _deps(session, diagnoses)
        attempt_id = uuid.uuid4()

        graph.run_attempt(
            deps,
            session_id=deps.recorder.session_id,
            problem=PROBLEM,
            student_answer="2",
            attempt_id=attempt_id,
        )

        assert len(diagnoses.logs) == 1
        recorded = diagnoses.logs[0]
        assert recorded["attempt_id"] == attempt_id
        assert recorded["tag"] == RULE_TAG
        assert recorded["source"] is DiagnosisSource.RULE
        # The identity that fired, not a summary of it — §3.1's pre-check is
        # evidence a teacher can check by hand.
        assert "7, 5 -> 2" in str(recorded["evidence"])

    def test_an_abstention_is_recorded_too(self, session: DbSession) -> None:
        """`unknown` is the majority of real diagnoses on fixture content.

        A table holding only the confident ones would report a diagnoser that is
        always right about everything it has an opinion on, while hiding how
        rarely it has one. §8 wants the `unknown` rate as a first-class metric,
        which means the abstention has to be a row.
        """
        seed(session)
        diagnoses = InMemoryDiagnosisSink()
        deps = _deps(session, diagnoses)  # no model: the shadow/provider-down path

        graph.run_attempt(
            deps,
            session_id=deps.recorder.session_id,
            problem=PROBLEM,
            student_answer=UNDIAGNOSABLE,
            attempt_id=uuid.uuid4(),
        )

        assert len(diagnoses.logs) == 1
        assert diagnoses.logs[0]["tag"] == UNKNOWN_TAG_LABEL
        assert diagnoses.logs[0]["confidence"] == 0.0

    def test_without_an_attempt_id_nothing_is_written(self, session: DbSession) -> None:
        """§5 keys `DiagnosisLog` by attempt. A caller with no attempt — a
        script, a test — gets the event log and no orphan row."""
        seed(session)
        diagnoses = InMemoryDiagnosisSink()
        deps = _deps(session, diagnoses)

        graph.run_attempt(
            deps, session_id=deps.recorder.session_id, problem=PROBLEM, student_answer="2"
        )

        assert diagnoses.logs == []

    def test_a_model_diagnosis_points_back_at_the_call_that_made_it(
        self, session: DbSession
    ) -> None:
        """M0.4's guarantee has two halves: the ledger records that a call
        happened, and the output points at it. Without the second, the recorded
        accuracy of the diagnoser cannot be attributed to a model version — which
        is the one thing calibration has to be able to say."""
        seed(session)
        diagnoses = InMemoryDiagnosisSink()
        llm = LLMClient(FakeTransport(responder=_responder(RULE_TAG, 0.82)), InMemoryLedger())
        deps = _deps(session, diagnoses, llm=llm)

        graph.run_attempt(
            deps,
            session_id=deps.recorder.session_id,
            problem=PROBLEM,
            student_answer=UNDIAGNOSABLE,
            attempt_id=uuid.uuid4(),
        )

        recorded = diagnoses.logs[0]
        assert recorded["source"] is DiagnosisSource.LLM
        assert recorded["llm_call_id"] is not None

    def test_a_low_confidence_reply_is_recorded_as_an_abstention(self, session: DbSession) -> None:
        """§3.1 would rather have no diagnosis than a wrong one, so a reply under
        the threshold becomes `unknown`. It is still a row: the rate at which the
        model answers below threshold is what P1.3 calibrates the threshold on."""
        seed(session)
        diagnoses = InMemoryDiagnosisSink()
        llm = LLMClient(FakeTransport(responder=_responder(RULE_TAG, 0.10)), InMemoryLedger())
        deps = _deps(session, diagnoses, llm=llm)

        graph.run_attempt(
            deps,
            session_id=deps.recorder.session_id,
            problem=PROBLEM,
            student_answer=UNDIAGNOSABLE,
            attempt_id=uuid.uuid4(),
        )

        recorded = diagnoses.logs[0]
        assert recorded["tag"] == UNKNOWN_TAG_LABEL
        assert recorded["source"] is DiagnosisSource.LLM
        assert recorded["llm_call_id"] is not None  # the abstention is attributable too


class TestTheTaxonomyIsNotExtendedAtRuntime:
    """§3.1 constrains the diagnoser's vocabulary to the authored taxonomy.

    P0.1 authors that taxonomy with a teacher, so growing it is not a runtime
    decision — and a sink that inserted a tag for every unfamiliar label would
    make it one.
    """

    def test_a_known_label_resolves_to_its_tag(self, session: DbSession) -> None:
        seed(session)
        expected = session.execute(
            select(t.MisconceptionTagRow.id).where(t.MisconceptionTagRow.label == RULE_TAG)
        ).scalar_one()

        log = DatabaseDiagnosisSink(session).record(
            attempt_id=uuid.uuid4(),
            tag=RULE_TAG,
            confidence=0.99,
            evidence="answer equals |a - b| on an addition problem",
            source=DiagnosisSource.RULE,
        )

        assert log.misconception_tag_id == expected

    def test_an_unrecognised_label_does_not_become_a_tag(self, session: DbSession) -> None:
        """The row is still written — as an abstention, so no downstream count
        treats it as a diagnosis — and the label is kept in `evidence` so an
        out-of-vocabulary reply stays measurable rather than being discarded."""
        seed(session)
        before = session.execute(select(func.count(t.MisconceptionTagRow.id))).scalar_one()

        log = DatabaseDiagnosisSink(session).record(
            attempt_id=uuid.uuid4(),
            tag="child_seemed_tired",
            confidence=0.9,
            evidence="model invented a tag",
            source=DiagnosisSource.RULE,
        )

        assert log.misconception_tag_id is None
        assert "child_seemed_tired" in log.evidence
        after = session.execute(select(func.count(t.MisconceptionTagRow.id))).scalar_one()
        assert after == before

    def test_unknown_is_an_abstention_and_not_an_unrecognised_label(
        self, session: DbSession
    ) -> None:
        """`unknown` has no taxonomy row and is not meant to have one, so it must
        not be annotated as if the diagnoser had gone off-vocabulary."""
        seed(session)

        log = DatabaseDiagnosisSink(session).record(
            attempt_id=uuid.uuid4(),
            tag=UNKNOWN_TAG_LABEL,
            confidence=0.0,
            evidence="no rule matched and no model was available",
            source=DiagnosisSource.RULE,
        )

        assert log.misconception_tag_id is None
        assert "unrecognised" not in log.evidence

    def test_the_row_lands_in_the_table(self, session: DbSession) -> None:
        """The whole point: a query, not a JSON scan of the timeline."""
        seed(session)
        attempt_id = uuid.uuid4()

        DatabaseDiagnosisSink(session).record(
            attempt_id=attempt_id,
            tag=RULE_TAG,
            confidence=0.99,
            evidence="answer equals |a - b| on an addition problem",
            source=DiagnosisSource.RULE,
        )

        rows = (
            session.execute(
                select(t.DiagnosisLogRow).where(t.DiagnosisLogRow.attempt_id == attempt_id)
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].confidence == 0.99

    def test_a_re_diagnosis_appends_rather_than_replaces(self, session: DbSession) -> None:
        """Append-only, like every log-bearing table (§5). The first row is what
        the child's hint was actually chosen from, and an audit that cannot see it
        cannot explain the hint."""
        seed(session)
        sink = DatabaseDiagnosisSink(session)
        attempt_id = uuid.uuid4()

        sink.record(
            attempt_id=attempt_id,
            tag=UNKNOWN_TAG_LABEL,
            confidence=0.0,
            evidence="first pass abstained",
            source=DiagnosisSource.RULE,
        )
        sink.record(
            attempt_id=attempt_id,
            tag=RULE_TAG,
            confidence=0.99,
            evidence="second pass matched a rule",
            source=DiagnosisSource.RULE,
        )

        rows = (
            session.execute(
                select(t.DiagnosisLogRow).where(t.DiagnosisLogRow.attempt_id == attempt_id)
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2
