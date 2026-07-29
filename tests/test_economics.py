"""Cost and latency from the ledger (§8, P1.10).

The numbers themselves are arithmetic. What these tests protect is the two
places the panel could mislead someone: a percentile computed from too few
calls, and a total that silently averages across prompt versions — which is the
exact thing §8 asks to be split, because a prompt edit can double spend without
changing anything a stage-level number would show.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.pool import StaticPool

from packages.domain import models as m
from packages.domain import tables as t
from packages.domain.enums import PipelineStage, Role
from packages.domain.mapping import to_row
from packages.telemetry.economics import MIN_FOR_P95, economics
from services.api.app import app
from services.api.auth import PRINCIPAL_HEADER
from services.api.db import get_db

NOW = dt.datetime(2026, 7, 29, tzinfo=dt.UTC)


def _call(
    db: DbSession,
    *,
    stage: PipelineStage,
    cost: float,
    latency: int,
    version: str = "v1",
    session_id: uuid.UUID | None = None,
    model: str = "claude-haiku-4-5",
) -> None:
    db.add(
        to_row(
            m.LLMCall(
                session_id=session_id or uuid.uuid4(),
                stage=stage,
                model_id=model,
                prompt_version=version,
                input_payload={},
                output_payload={},
                tokens_in=100,
                tokens_out=10,
                latency_ms=latency,
                cost_usd=cost,
                created_at=NOW,
            ),
            t.LLMCallRow,
        )
    )


class TestTheNumbers:
    def test_spend_is_split_by_stage_and_shares_sum_to_one(self, session: DbSession) -> None:
        _call(session, stage=PipelineStage.GENERATE_HINT, cost=0.003, latency=1800)
        _call(session, stage=PipelineStage.LEAK_CHECK, cost=0.001, latency=1200)
        session.flush()

        result = economics(session)

        assert result.total_cost_usd == pytest.approx(0.004)
        assert [s.stage for s in result.by_stage] == ["generate_hint", "leak_check"]
        assert sum(s.share_of_cost for s in result.by_stage) == pytest.approx(1.0)

    def test_cost_per_session_aggregates_across_that_session_s_calls(
        self, session: DbSession
    ) -> None:
        """The number an operator budgets against is per child, not per call."""
        one, two = uuid.uuid4(), uuid.uuid4()
        _call(session, stage=PipelineStage.DIAGNOSE, cost=0.001, latency=900, session_id=one)
        _call(session, stage=PipelineStage.GENERATE_HINT, cost=0.004, latency=900, session_id=one)
        _call(session, stage=PipelineStage.DIAGNOSE, cost=0.001, latency=900, session_id=two)
        session.flush()

        result = economics(session)

        assert result.sessions == 2
        assert result.cost_per_session_max == pytest.approx(0.005)

    def test_an_empty_ledger_does_not_divide_by_zero(self, session: DbSession) -> None:
        result = economics(session)

        assert result.calls == 0
        assert result.total_cost_usd == 0.0
        assert result.by_stage == []


class TestItRefusesToOverstate:
    def test_p95_is_withheld_below_the_sample_threshold(self, session: DbSession) -> None:
        """A p95 over nine calls is the second-slowest call wearing a
        statistic's name — and this panel is exactly where someone would quote
        it from."""
        for i in range(MIN_FOR_P95 - 1):
            _call(session, stage=PipelineStage.DIAGNOSE, cost=0.001, latency=100 + i)
        session.flush()

        stage = economics(session).by_stage[0]

        assert stage.calls == MIN_FOR_P95 - 1
        assert stage.latency_p95_ms is None
        assert stage.latency_p50_ms > 0, "the median is still reportable"

    def test_p95_appears_once_there_are_enough_calls(self, session: DbSession) -> None:
        for i in range(MIN_FOR_P95):
            _call(session, stage=PipelineStage.DIAGNOSE, cost=0.001, latency=100 + i)
        session.flush()

        stage = economics(session).by_stage[0]

        assert stage.latency_p95_ms is not None
        assert stage.latency_p95_ms >= stage.latency_p50_ms


class TestPromptVersionSegmentation:
    def test_two_versions_of_one_stage_are_reported_separately(self, session: DbSession) -> None:
        """The §8 requirement, and the reason for it: a v2 that costs four times
        v1 is invisible in a stage total that averages them."""
        for _ in range(3):
            _call(session, stage=PipelineStage.DIAGNOSE, cost=0.001, latency=900, version="v1")
        _call(session, stage=PipelineStage.DIAGNOSE, cost=0.004, latency=3000, version="v2")
        session.flush()

        result = economics(session)

        assert len(result.by_stage) == 1, "one stage"
        versions = {s.prompt_version: s for s in result.by_prompt_version}
        assert set(versions) == {"v1", "v2"}
        assert versions["v2"].cost_usd > versions["v1"].cost_usd
        # The stage total hides it; the split is what surfaces it.
        assert result.by_stage[0].cost_usd == pytest.approx(0.007)


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
    p = m.Principal(role=role, display_name=role.value, created_at=NOW)
    db.add(to_row(p, t.PrincipalRow))
    db.commit()
    return p.id


class TestTheEndpoint:
    def test_an_admin_sees_the_numbers(self, client: TestClient, api_db: DbSession) -> None:
        admin = _principal(api_db, Role.ADMIN)
        _call(api_db, stage=PipelineStage.GENERATE_HINT, cost=0.003, latency=1800)
        api_db.commit()

        response = client.get("/admin/economics", headers={PRINCIPAL_HEADER: str(admin)})

        assert response.status_code == 200
        body = response.json()
        assert body["total_cost_usd"] == pytest.approx(0.003)
        assert body["by_stage"][0]["stage"] == "generate_hint"

    def test_a_teacher_cannot_read_deployment_wide_spend(
        self, client: TestClient, api_db: DbSession
    ) -> None:
        """Spend spans every child, so it follows the run list's rule rather
        than the per-session one."""
        teacher = _principal(api_db, Role.TEACHER)

        response = client.get("/admin/economics", headers={PRINCIPAL_HEADER: str(teacher)})

        assert response.status_code == 403
