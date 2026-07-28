"""The Phase 0 readiness gates.

The load-bearing test is `test_an_empty_database_is_never_ready`. A report that
reads "all gates green" over no data would be the most expensive bug in this
repo, because the decision it unblocks is showing model-generated text to
children. Everything else here exists to make sure each gate fails for its own
reason rather than by accident.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy.orm import Session as DbSession

from eval.phase0 import (
    MIN_REVIEWED_FOR_A_RATE,
    MIN_SHADOW_WIN_RATE,
    Gate,
    GateStatus,
    Phase0Report,
    build_report,
)
from packages.domain import models as m
from packages.domain import tables as t
from packages.domain.enums import EventType, PipelineStage, Role
from packages.domain.mapping import to_row

NOW = dt.datetime(2026, 7, 28, tzinfo=dt.UTC)


def _gate(report: Phase0Report, name: str) -> Gate:
    return next(g for g in report.gates if g.name == name)


class Fixture:
    """Builds pilot data of whatever shape a test needs."""

    def __init__(self, db: DbSession) -> None:
        self.db = db
        self.teacher = m.Principal(role=Role.TEACHER, display_name="T", created_at=NOW)
        db.add(to_row(self.teacher, t.PrincipalRow))
        node = m.CurriculumNode(
            standard_code="1.OA.C.6", grade_band="K-1", definition="Add within 20."
        )
        db.add(to_row(node, t.CurriculumNodeRow))
        db.flush()
        self.problem = m.Problem(
            curriculum_node_id=node.id,
            prompt="What is 7 + 5?",
            correct_answer="12",
            answer_type="numeric",
            grade_band="K-1",
        )
        db.add(to_row(self.problem, t.ProblemRow))
        db.flush()
        db.commit()

    def session(self) -> uuid.UUID:
        student = m.Student(grade_level=1, created_at=NOW)
        self.db.add(to_row(student, t.StudentRow))
        self.db.flush()
        session = m.Session(
            student_id=student.id, problem_id=self.problem.id, started_at=NOW, attempt_count=1
        )
        self.db.add(to_row(session, t.SessionRow))
        self.db.flush()
        return session.id

    def candidate(self, *, leak_passed: bool = True) -> uuid.UUID:
        candidate = m.ShadowCandidate(
            session_id=self.session(),
            attempt_number=1,
            hint_level=1,
            generated_text="Fill your ten-frame with 7.",
            shown_text="You have 7 and are getting 5 more.",
            misconception_tag="subtracted_instead_of_added",
            prompt_version="generate_hint/K-1/v1",
            leak_check_passed=leak_passed,
            leak_checker_version="deterministic/v1",
            created_at=NOW,
        )
        self.db.add(to_row(candidate, t.ShadowCandidateRow))
        self.db.flush()
        return candidate.id

    def rate(
        self,
        candidate_id: uuid.UUID,
        *,
        better: bool = True,
        would_leak: bool = False,
        corrected: bool = False,
    ) -> None:
        self.db.add(
            to_row(
                m.ShadowRating(
                    shadow_candidate_id=candidate_id,
                    teacher_id=self.teacher.id,
                    better_than_shown=better,
                    would_leak=would_leak,
                    notes="[corrected tag: counted_on_from_wrong_start]" if corrected else None,
                    created_at=NOW,
                ),
                t.ShadowRatingRow,
            )
        )
        self.db.flush()

    def rate_many(self, count: int, *, better: int, corrected: int = 0) -> None:
        for index in range(count):
            self.rate(
                self.candidate(),
                better=index < better,
                corrected=index < corrected,
            )
        self.db.commit()

    def fallback(self, stage: PipelineStage, sequence: int) -> None:
        self.db.add(
            to_row(
                m.PipelineEvent(
                    session_id=self.session(),
                    sequence=sequence,
                    event_type=EventType.FALLBACK_USED,
                    stage=stage,
                    detail={"reason": "provider down"},
                    occurred_at=NOW,
                ),
                t.PipelineEventRow,
            )
        )
        self.db.commit()


@pytest.fixture
def pilot(session: DbSession) -> Fixture:
    return Fixture(session)


class TestEmptyIsNeverReady:
    def test_an_empty_database_is_never_ready(self, session: DbSession) -> None:
        """The bug this whole module exists to prevent."""
        report = build_report(session)
        assert report.ready is False

    def test_every_data_gate_reads_insufficient_not_pass(self, session: DbSession) -> None:
        """ "No evidence" must never render as "no problem"."""
        report = build_report(session)
        data_gates = [g for g in report.gates if g.name not in {"compliance sign-off"}]
        assert all(g.status is GateStatus.INSUFFICIENT for g in data_gates), [
            (g.name, g.status) for g in data_gates
        ]

    def test_compliance_is_reported_as_a_human_gate(self, session: DbSession) -> None:
        """Not computable, and not quietly omitted either."""
        gate = _gate(build_report(session), "compliance sign-off")
        assert gate.status is GateStatus.MANUAL
        assert gate.blocks_exit is True


class TestSmallSamplesDoNotConclude:
    def test_a_handful_of_ratings_is_insufficient_not_a_pass(self, pilot: Fixture) -> None:
        """Three ratings at 100% is exactly the number that gets quoted in a
        go/no-go meeting. It should not be available to quote."""
        pilot.rate_many(3, better=3)

        gate = _gate(build_report(pilot.db), "generation beats template")
        assert gate.status is GateStatus.INSUFFICIENT
        assert "too few" in gate.detail

    def test_enough_ratings_above_the_bar_passes(self, pilot: Fixture) -> None:
        pilot.rate_many(40, better=32)  # 80%

        gate = _gate(build_report(pilot.db), "generation beats template")
        assert gate.status is GateStatus.PASS

    def test_enough_ratings_below_the_bar_fails(self, pilot: Fixture) -> None:
        pilot.rate_many(40, better=20)  # 50%, below the 70% bar

        gate = _gate(build_report(pilot.db), "generation beats template")
        assert gate.status is GateStatus.FAIL
        assert f"{MIN_SHADOW_WIN_RATE:.0%}" in gate.detail


class TestLeakSafety:
    def test_a_teacher_flagged_leak_fails_the_gate(self, pilot: Fixture) -> None:
        """A hint the checker passed and a teacher judged leaky is the checker
        missing one — the single most serious finding the pilot can produce."""
        candidate = pilot.candidate(leak_passed=True)
        pilot.rate(candidate, would_leak=True)
        pilot.db.commit()

        gate = _gate(build_report(pilot.db), "leak safety")
        assert gate.status is GateStatus.FAIL
        assert "leak_corpus" in gate.detail  # tells the reader what to do next

    def test_checker_catching_leaks_is_not_a_failure(self, pilot: Fixture) -> None:
        """The checker doing its job is the system working, not a problem."""
        pilot.candidate(leak_passed=False)
        pilot.rate_many(MIN_REVIEWED_FOR_A_RATE, better=MIN_REVIEWED_FOR_A_RATE)

        gate = _gate(build_report(pilot.db), "leak safety")
        assert gate.status is GateStatus.PASS

    def test_no_leaks_over_a_few_reviews_is_not_a_pass(self, pilot: Fixture) -> None:
        """Nobody flagged a leak because almost nobody looked.

        This is the same mistake as a 100% win rate off three ratings, with the
        highest stakes of any gate here: it would be read as "the checker holds"
        when it means "the checker is untested against real model output".
        """
        pilot.rate(pilot.candidate(), would_leak=False)
        pilot.db.commit()

        gate = _gate(build_report(pilot.db), "leak safety")
        assert gate.status is GateStatus.INSUFFICIENT
        assert "nobody having looked" in gate.detail

    def test_one_flagged_leak_fails_even_on_thin_data(self, pilot: Fixture) -> None:
        """A miss is a miss. Volume gates the absence of evidence, not its presence."""
        pilot.rate(pilot.candidate(leak_passed=True), would_leak=True)
        pilot.db.commit()

        gate = _gate(build_report(pilot.db), "leak safety")
        assert gate.status is GateStatus.FAIL


class TestDegradation:
    def test_partial_coverage_fails_and_names_the_gap(self, pilot: Fixture) -> None:
        pilot.fallback(PipelineStage.DIAGNOSE, 0)

        gate = _gate(build_report(pilot.db), "degradation paths")
        assert gate.status is GateStatus.FAIL
        assert "leak_check" in gate.detail

    def test_every_stage_exercised_passes(self, pilot: Fixture) -> None:
        for index, stage in enumerate(
            (
                PipelineStage.DIAGNOSE,
                PipelineStage.RERANK,
                PipelineStage.GENERATE_HINT,
                PipelineStage.LEAK_CHECK,
                PipelineStage.SAFETY_SCREEN,
            )
        ):
            pilot.fallback(stage, index)

        gate = _gate(build_report(pilot.db), "degradation paths")
        assert gate.status is GateStatus.PASS


class TestRendering:
    def test_not_ready_says_shadow_mode_stays_on(self, session: DbSession) -> None:
        """The report's job is a decision, not a table of numbers."""
        rendered = build_report(session).render()
        assert "NOT READY" in rendered
        assert "Shadow mode stays on" in rendered

    def test_notes_are_separate_from_gates(self, pilot: Fixture) -> None:
        """A number that cannot fail does not belong in a pass/fail list."""
        report = build_report(pilot.db)
        assert any("cost" in note for note in report.notes)
        assert not any("cost" in g.name for g in report.gates)
