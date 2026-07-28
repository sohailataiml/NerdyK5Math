"""P1.1 — the staged rollout and the kill switch.

Implementation-Plan.md P1.1 asks for generated hints at 5% -> 25% -> 100% of
sessions with a "kill switch [that] reverts to templates instantly, tested before
launch, not after". This file is the "before launch" half of that sentence: the
switch is exercised here against the real pipeline and the real table, not
described in a runbook and discovered to be broken during the incident it exists
for.

Four properties carry the weight, and each has a failure mode worth naming:

- **Killing generation takes effect on the next child.** If it needed a restart
  it would not be a kill switch, and the gap between deciding to stop and
  stopping would be measured in children.
- **A session's cohort never changes.** A tutor that generates hint 1 and
  templates hint 2 reads to a child as the tutor changing its mind about them.
- **Advancing the rollout evicts nobody.** Otherwise the teacher ratings that
  justified 5% -> 25% describe a cohort that no longer exists.
- **Withholding is not degradation.** During a 5% rollout, 95% of sessions serve
  templates by design; logged as fallbacks they would drown every real outage.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.pool import StaticPool

from packages.curriculum.seed import seed
from packages.domain import models as m
from packages.domain import tables as t
from packages.domain.append_only import AppendOnlyError
from packages.domain.enums import GradeBand, HintSource, PipelineStage, Role
from packages.domain.mapping import to_row
from packages.llm import LLMClient
from packages.llm.fake import FakeTransport
from packages.llm.ledger import InMemoryLedger
from packages.prompts import PromptRegistry
from packages.telemetry import EventRecorder, InMemoryEventSink
from services.api.app import app
from services.api.auth import PRINCIPAL_HEADER
from services.api.db import get_db
from services.orchestrator import graph
from services.orchestrator.rollout import (
    BUCKETS,
    DatabaseRolloutSource,
    RolloutState,
    StaticRollout,
    bucket_for,
    decide,
    history,
    record_change,
)
from services.orchestrator.state import PipelineDeps, Problem

PROBLEM = Problem(
    prompt="What is 7 + 5?",
    correct_answer="12",
    grade_band=GradeBand.K_1,
    operands={"a": "7", "b": "5"},
)

SAFE_HINT = "Fill your ten-frame with 7 counters. How many more to make ten?"

# Two sessions with known, opposite cohort positions. Hard-coded rather than
# searched for at test time so a change to the bucketing function shows up here
# as a failure rather than as a silently reshuffled pilot.
IN_COHORT = uuid.UUID("00000000-0000-0000-0000-000000000001")  # bucket 2
OUT_OF_COHORT = uuid.UUID("11111111-1111-1111-1111-111111111111")  # bucket 51


def _responder(hint: str) -> Callable[[str, str], str]:
    def respond(system: str, _user: str) -> str:
        if "gives away the answer" in system:
            return "SAFE"
        return hint

    return respond


def _deps(
    db: DbSession,
    *,
    rollout: object | None = None,
    reply: str = SAFE_HINT,
) -> tuple[PipelineDeps, InMemoryLedger, InMemoryEventSink]:
    """A live (non-shadow) pipeline, which is the only place a rollout applies."""
    sink = InMemoryEventSink()
    ledger = InMemoryLedger()
    deps = PipelineDeps(
        recorder=EventRecorder(sink, uuid.uuid4()),
        prompts=PromptRegistry(),
        llm=LLMClient(FakeTransport(responder=_responder(reply)), ledger),
        db=db,
        shadow_mode=False,
        rollout=rollout,  # type: ignore[arg-type]
    )
    return deps, ledger, sink


class TestCohortAssignment:
    def test_bucket_is_pinned_to_a_fixed_digest(self) -> None:
        """Not `hash()`.

        Python salts hashing per process, so the same session would land in a
        different cohort in each worker and again after every restart: a child
        flipping between generated and template hints request to request, and a
        "5% cohort" that is a different 5% on every deploy. Every rating
        collected against it would then describe a population that no longer
        exists. These constants are what makes that regression visible.
        """
        assert bucket_for(IN_COHORT) == 2
        assert bucket_for(OUT_OF_COHORT) == 51

    def test_bucket_is_stable_across_calls(self) -> None:
        session_id = uuid.uuid4()
        assert len({bucket_for(session_id) for _ in range(20)}) == 1

    def test_every_bucket_is_in_range(self) -> None:
        assert all(0 <= bucket_for(uuid.uuid4()) < BUCKETS for _ in range(2_000))

    def test_the_split_is_close_to_the_percentage(self) -> None:
        """5% has to mean 5%. A pilot classroom is small enough that "roughly"
        can be the difference between some children in the cohort and none."""
        sessions = [uuid.uuid4() for _ in range(20_000)]
        state = RolloutState(generation_enabled=True, percentage=5)
        served = sum(1 for s in sessions if decide(state, session_id=s).serve_generated)

        assert 0.04 < served / len(sessions) < 0.06

    def test_advancing_the_rollout_never_evicts_a_session(self) -> None:
        """5% -> 25% must add sessions and remove none.

        A fresh draw per step would mean the ratings that justified advancing
        describe a cohort that has since been reshuffled — quietly destroying the
        evidence the gate was decided on.
        """
        sessions = [uuid.uuid4() for _ in range(500)]

        def cohort(percentage: int) -> set[uuid.UUID]:
            state = RolloutState(generation_enabled=True, percentage=percentage)
            return {s for s in sessions if decide(state, session_id=s).serve_generated}

        assert cohort(5) <= cohort(25) <= cohort(100)


class TestDecide:
    def test_zero_percent_serves_nobody(self) -> None:
        state = RolloutState(generation_enabled=True, percentage=0)
        assert not decide(state, session_id=IN_COHORT).serve_generated

    def test_one_hundred_percent_serves_everybody(self) -> None:
        state = RolloutState(generation_enabled=True, percentage=100)
        assert all(decide(state, session_id=uuid.uuid4()).serve_generated for _ in range(200))

    def test_the_kill_switch_beats_the_percentage(self) -> None:
        """Reverting is one field. If the switch had to be combined with zeroing
        the percentage, a rollback would be two edits under pressure and the
        second one is the one that gets forgotten."""
        state = RolloutState(generation_enabled=False, percentage=100, reason="leak reported")
        decision = decide(state, session_id=IN_COHORT)

        assert not decision.serve_generated
        assert "switched off" in decision.reason
        assert "leak reported" in decision.reason  # the operator's words survive
        assert decision.percentage == 100  # the cohort is preserved for the restart

    def test_a_percentage_outside_the_range_is_refused(self) -> None:
        with pytest.raises(ValueError, match="0-100"):
            RolloutState(generation_enabled=True, percentage=101)


class TestUnconfiguredDeploymentServesTemplates:
    def test_an_empty_table_means_generation_is_off(self, session: DbSession) -> None:
        """Phase 1 starts at 5% *after* Phase 0's gates are met. A deployment
        never told otherwise has not met them, and the default that costs least
        when wrong is the one that shows a teacher-approved template."""
        state = DatabaseRolloutSource(session).current()

        assert state.generation_enabled is False
        assert state.percentage == 0
        assert state.reason == "no rollout has been configured"

    def test_leaving_shadow_mode_is_not_enough_to_reach_a_child(self, session: DbSession) -> None:
        """The two switches are separate decisions. Ending Phase 0 does not, by
        itself, put a model's words in front of anyone."""
        seed(session)
        deps, _, _ = _deps(session, rollout=DatabaseRolloutSource(session))

        result = graph.run_attempt(deps, session_id=IN_COHORT, problem=PROBLEM, student_answer="2")

        assert result.hint_source is HintSource.TEMPLATE_FALLBACK
        assert result.rollout is not None
        assert not result.rollout.serve_generated


class TestTheKillSwitchIsLive:
    """P1.1's "instantly", tested rather than asserted in a runbook."""

    def test_killing_generation_changes_the_very_next_attempt(self, session: DbSession) -> None:
        seed(session)
        source = DatabaseRolloutSource(session)
        operator = uuid.uuid4()
        record_change(
            session,
            generation_enabled=True,
            percentage=100,
            changed_by=operator,
            reason="Phase 0 gates met.",
        )

        deps, _, _ = _deps(session, rollout=source)
        before = graph.run_attempt(deps, session_id=IN_COHORT, problem=PROBLEM, student_answer="2")
        assert before.hint_source is HintSource.GENERATED

        # No restart, no redeploy, no new dependency graph — the same `deps`.
        record_change(
            session,
            generation_enabled=False,
            percentage=100,
            changed_by=operator,
            reason="Teacher flagged a leaked answer.",
        )

        after = graph.run_attempt(deps, session_id=IN_COHORT, problem=PROBLEM, student_answer="2")
        assert after.hint_source is HintSource.TEMPLATE_FALLBACK
        assert after.hint_text  # the child is still taught, from the template library

    def test_the_window_generation_was_on_survives_the_rollback(self, session: DbSession) -> None:
        """Append-only, so an incident review can still see what was live and
        when. A mutable settings row would have overwritten exactly the record
        the review needs."""
        operator = uuid.uuid4()
        record_change(
            session,
            generation_enabled=True,
            percentage=25,
            changed_by=operator,
            reason="Advancing after a clean week at 5%.",
        )
        record_change(
            session,
            generation_enabled=False,
            percentage=25,
            changed_by=operator,
            reason="Leak reported in 4B.",
        )

        trail = history(session)
        assert [c.generation_enabled for c in trail] == [False, True]
        assert trail[1].reason == "Advancing after a clean week at 5%."

    def test_a_recorded_change_cannot_be_edited_away(self, session: DbSession) -> None:
        record_change(
            session,
            generation_enabled=True,
            percentage=100,
            changed_by=uuid.uuid4(),
            reason="Full rollout.",
        )
        session.commit()
        row = session.query(t.RolloutChangeRow).one()

        row.percentage = 0
        with pytest.raises(AppendOnlyError):
            session.flush()

    def test_a_tie_resolves_toward_less_generation(self, session: DbSession) -> None:
        """Two changes can share a timestamp, and the table has no sequence to
        fall back on. A kill switch racing an unrelated percentage bump must not
        lose the coin toss — being wrong here costs a template hint, and being
        wrong the other way costs a leak."""
        stamp = dt.datetime(2026, 7, 28, 9, 0, 0, tzinfo=dt.UTC)
        for enabled, pct in ((True, 100), (False, 100)):
            change = m.RolloutChange(
                generation_enabled=enabled,
                percentage=pct,
                changed_by=uuid.uuid4(),
                reason="simultaneous",
                created_at=stamp,
            )
            session.add(to_row(change, t.RolloutChangeRow))
        session.flush()

        assert DatabaseRolloutSource(session).current().generation_enabled is False


class TestASessionsCohortNeverChanges:
    def test_every_hint_level_in_a_session_resolves_the_same_way(self, session: DbSession) -> None:
        """A tutor that generates hint 1 and templates hint 2 reads to a child as
        the tutor changing its mind about them, and to a teacher rating the
        session as noise."""
        seed(session)
        rollout = StaticRollout(RolloutState(generation_enabled=True, percentage=50))
        deps, _, _ = _deps(session, rollout=rollout)

        sources = {
            graph.run_attempt(
                deps,
                session_id=IN_COHORT,
                problem=PROBLEM,
                student_answer="2",
                hint_level=level,
                attempt=level,
            ).hint_source
            for level in (1, 2, 3)
        }

        assert sources == {HintSource.GENERATED}

    def test_a_session_outside_the_cohort_stays_outside_it(self, session: DbSession) -> None:
        seed(session)
        rollout = StaticRollout(RolloutState(generation_enabled=True, percentage=50))
        deps, _, _ = _deps(session, rollout=rollout)

        sources = {
            graph.run_attempt(
                deps,
                session_id=OUT_OF_COHORT,
                problem=PROBLEM,
                student_answer="2",
                hint_level=level,
                attempt=level,
            ).hint_source
            for level in (1, 2, 3)
        }

        assert sources == {HintSource.TEMPLATE_FALLBACK}


class TestWithholdingIsNotDegradation:
    def test_an_out_of_cohort_session_is_not_marked_degraded(self, session: DbSession) -> None:
        """During a 5% rollout this is 95% of sessions. Marked as fallbacks they
        would put a working rollout in the same bucket as a provider outage, and
        §8's dashboards would read a healthy system as broken — which is how the
        real outage gets missed inside the noise."""
        seed(session)
        rollout = StaticRollout(RolloutState(generation_enabled=True, percentage=5))
        deps, _, _ = _deps(session, rollout=rollout)

        result = graph.run_attempt(
            deps, session_id=OUT_OF_COHORT, problem=PROBLEM, student_answer="2"
        )

        assert result.hint_source is HintSource.TEMPLATE_FALLBACK
        assert "generate" not in result.degraded_stages
        assert result.ran_degraded is False

    def test_the_record_names_the_rollout_as_the_reason(self, session: DbSession) -> None:
        """Four causes produce the same hint text — shadow mode, an outage, the
        leak check, and this. Reading one as another misstates what happened in a
        child's session."""
        seed(session)
        rollout = StaticRollout(RolloutState(generation_enabled=True, percentage=5))
        deps, _, sink = _deps(session, rollout=rollout)

        graph.run_attempt(deps, session_id=OUT_OF_COHORT, problem=PROBLEM, student_answer="2")

        reasons = [
            str(e.detail.get("reason", ""))
            for e in sink.events
            if e.event_type.value == "fallback_used"
        ]
        assert any("rollout" in r and "outside" in r for r in reasons)
        assert not any("leak-check failures" in r for r in reasons)

    def test_an_out_of_cohort_session_is_not_billed_for_generation(
        self, session: DbSession
    ) -> None:
        """A session that cannot be shown generated text should not pay to
        generate it (P1.10). Cost scales with engagement, and a 5% rollout that
        bills for 100% of hints is a 20x overspend on the phase's own terms.

        The leak check is deliberately *not* skipped alongside it. A template is
        pre-approved as a form, not as a finished string: it is rendered with this
        problem's operands, and `packages/fallbacks/answer_leak.py` exists because
        that rendering can produce the answer. Withholding generation is a cost
        and rollout decision; the guardrail is neither.
        """
        seed(session)
        rollout = StaticRollout(RolloutState(generation_enabled=True, percentage=5))
        deps, ledger, _ = _deps(session, rollout=rollout)

        graph.run_attempt(deps, session_id=OUT_OF_COHORT, problem=PROBLEM, student_answer="2")

        stages = [call.stage for call in ledger.calls]
        assert PipelineStage.GENERATE_HINT not in stages
        assert PipelineStage.LEAK_CHECK in stages

    def test_shadow_mode_still_wins_over_a_full_rollout(self, session: DbSession) -> None:
        """The two gates compose in the safe direction. A rollout at 100% does not
        end Phase 0 — that is `shadow_mode`, and it is a different decision made
        against different evidence."""
        seed(session)
        deps, _, _ = _deps(
            session,
            rollout=StaticRollout(RolloutState(generation_enabled=True, percentage=100)),
        )
        deps.shadow_mode = True
        from services.orchestrator.shadow import InMemoryShadowSink

        deps.shadow_sink = InMemoryShadowSink()

        result = graph.run_attempt(deps, session_id=IN_COHORT, problem=PROBLEM, student_answer="2")

        assert result.shadow_ran is True
        assert result.hint_source is HintSource.TEMPLATE_FALLBACK


# ---------------------------------------------------------------------------
# The operator surface
# ---------------------------------------------------------------------------


@pytest.fixture
def api_db() -> Iterator[DbSession]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    t.Base.metadata.create_all(engine)
    with DbSession(engine) as db:
        yield db
    engine.dispose()


@pytest.fixture
def client(api_db: DbSession) -> Iterator[TestClient]:
    app.dependency_overrides[get_db] = lambda: api_db
    yield TestClient(app)
    app.dependency_overrides.clear()


def _principal(db: DbSession, role: Role) -> uuid.UUID:
    student_id = None
    if role is Role.STUDENT:
        student = m.Student(grade_level=1, created_at=dt.datetime.now(dt.UTC))
        db.add(to_row(student, t.StudentRow))
        db.flush()
        student_id = student.id
    principal = m.Principal(
        role=role,
        display_name=f"{role.value} one",
        student_id=student_id,
        created_at=dt.datetime.now(dt.UTC),
    )
    db.add(to_row(principal, t.PrincipalRow))
    db.commit()
    return principal.id


class TestRolloutEndpoint:
    def test_an_admin_can_read_and_advance_the_rollout(
        self, client: TestClient, api_db: DbSession
    ) -> None:
        admin = _principal(api_db, Role.ADMIN)
        headers = {PRINCIPAL_HEADER: str(admin)}

        initial = client.get("/admin/rollout", headers=headers).json()
        assert initial["configured"] is False
        assert initial["generation_enabled"] is False

        response = client.post(
            "/admin/rollout",
            headers=headers,
            json={
                "generation_enabled": True,
                "percentage": 5,
                "reason": "Phase 0 exit gates met; opening to 5%.",
            },
        )

        assert response.status_code == 201
        assert response.json() == {
            "generation_enabled": True,
            "percentage": 5,
            "reason": "Phase 0 exit gates met; opening to 5%.",
            "configured": True,
        }

    def test_kill_preserves_the_cohort(self, client: TestClient, api_db: DbSession) -> None:
        """One field under pressure, and the percentage someone reasoned their
        way to survives — so restarting after the incident does not begin with a
        number typed from memory."""
        admin = _principal(api_db, Role.ADMIN)
        headers = {PRINCIPAL_HEADER: str(admin)}
        client.post(
            "/admin/rollout",
            headers=headers,
            json={"generation_enabled": True, "percentage": 25, "reason": "Advancing."},
        )

        killed = client.post(
            "/admin/rollout/kill", headers=headers, json={"reason": "Leak in 4B."}
        ).json()

        assert killed["generation_enabled"] is False
        assert killed["percentage"] == 25

    def test_a_change_is_attributed_to_whoever_made_it(
        self, client: TestClient, api_db: DbSession
    ) -> None:
        """Phase 1's exit criteria are argued from what happened during each
        step. An unattributed percentage change makes that unreconstructable."""
        admin = _principal(api_db, Role.ADMIN)
        headers = {PRINCIPAL_HEADER: str(admin)}
        client.post(
            "/admin/rollout",
            headers=headers,
            json={"generation_enabled": True, "percentage": 5, "reason": "Opening to 5%."},
        )

        trail = client.get("/admin/rollout/history", headers=headers).json()

        assert len(trail) == 1
        assert trail[0]["changed_by"] == str(admin)
        assert trail[0]["reason"] == "Opening to 5%."

    def test_a_change_without_a_reason_is_refused(
        self, client: TestClient, api_db: DbSession
    ) -> None:
        """A change to what every child is shown that nobody wrote a sentence
        about is a change nobody can review."""
        admin = _principal(api_db, Role.ADMIN)

        response = client.post(
            "/admin/rollout",
            headers={PRINCIPAL_HEADER: str(admin)},
            json={"generation_enabled": True, "percentage": 5, "reason": "   "},
        )

        assert response.status_code == 422

    @pytest.mark.parametrize("role", [Role.TEACHER, Role.STUDENT])
    def test_only_an_admin_may_touch_the_rollout(
        self, client: TestClient, api_db: DbSession, role: Role
    ) -> None:
        """One setting for the whole deployment, so anyone who can change it
        changes it for every child. Granting that to a classroom role would hand
        a system-wide switch to whoever has the most students."""
        principal = _principal(api_db, role)
        headers = {PRINCIPAL_HEADER: str(principal)}

        assert client.get("/admin/rollout", headers=headers).status_code == 403
        assert (
            client.post(
                "/admin/rollout",
                headers=headers,
                json={"generation_enabled": True, "percentage": 100, "reason": "no"},
            ).status_code
            == 403
        )
        assert (
            client.post("/admin/rollout/kill", headers=headers, json={"reason": "no"}).status_code
            == 403
        )

    def test_an_unauthenticated_caller_is_refused(self, client: TestClient) -> None:
        assert client.get("/admin/rollout").status_code == 401
