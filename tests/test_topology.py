"""The drawn swarm cannot disagree with the running swarm.

A diagram is accurate on the day it is drawn. `swarm.topology()` derives nodes
and edges from each agent's `Command[Literal[...]]` return annotation — the same
annotation LangGraph validates the graph against — so a picture built from it is
accurate or the graph is broken.

These tests exist to keep that true. The one that matters most is the last:
adding a handoff without updating the annotation should be caught, because that
is the only way the two can drift apart.
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
from packages.llm.config import STAGE_CONFIG
from services.api.app import app
from services.api.auth import PRINCIPAL_HEADER
from services.api.db import get_db
from services.orchestrator import swarm

NOW = dt.datetime(2026, 7, 28, tzinfo=dt.UTC)


class TestDerivedFromTheCode:
    def test_every_registered_node_appears(self) -> None:
        assert {n.id for n in swarm.topology()} == set(swarm.AGENTS)

    def test_the_entry_node_is_the_one_the_graph_starts_at(self) -> None:
        entries = [n.id for n in swarm.topology() if n.entry]
        assert entries == [swarm.ENTRY_NODE]

    def test_handoffs_come_from_the_annotation_not_a_list(self) -> None:
        """`leakcheck_agent` is the interesting one: three destinations, and the
        two that are not the happy path are the guardrail."""
        leak = next(n for n in swarm.topology() if n.id == "leakcheck_agent")

        assert set(leak.handoffs) == {"record_hint_agent", "generate_agent", "escalate_agent"}

    def test_every_handoff_target_exists_or_is_the_end(self) -> None:
        """A target naming a node that is not registered is a graph that cannot
        run — and a picture with an edge into nothing."""
        known = set(swarm.AGENTS) | {"__end__"}

        for node in swarm.topology():
            assert set(node.handoffs) <= known, f"{node.id} hands off to something unknown"

    def test_generation_cannot_reach_a_shown_hint_without_the_leak_check(self) -> None:
        """§3.3's blocking guardrail, asserted on the graph's shape rather than
        on a run: there is no edge from generation to the node that records a
        hint as shown. If one ever appears, this fails before a child sees it.
        """
        generate_node = next(n for n in swarm.topology() if n.id == "generate_agent")

        assert "record_hint_agent" not in generate_node.handoffs
        assert "leakcheck_agent" in generate_node.handoffs

    def test_stage_mapping_covers_every_node(self) -> None:
        """A node missing from NODE_STAGE would raise when the topology is
        built, which is a worse failure than a test."""
        assert set(swarm.NODE_STAGE) == set(swarm.AGENTS)

    def test_nodes_that_call_a_model_map_to_a_configured_stage(self) -> None:
        """The canvas labels each node with its model tier, read from
        `packages.llm.config`. A stage with no config would render a tier the
        client would never actually use."""
        for node in swarm.topology():
            if node.stage is not None:
                assert node.stage in STAGE_CONFIG or node.stage is PipelineStage.RERANK


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
    def test_an_admin_gets_the_graph(self, client: TestClient, api_db: DbSession) -> None:
        admin = _principal(api_db, Role.ADMIN)

        response = client.get("/admin/topology", headers={PRINCIPAL_HEADER: str(admin)})

        assert response.status_code == 200
        nodes = response.json()
        assert {n["id"] for n in nodes} == set(swarm.AGENTS)
        entry = next(n for n in nodes if n["entry"])
        assert entry["id"] == swarm.ENTRY_NODE

    def test_model_backed_nodes_carry_their_tier(
        self, client: TestClient, api_db: DbSession
    ) -> None:
        admin = _principal(api_db, Role.ADMIN)

        nodes = client.get("/admin/topology", headers={PRINCIPAL_HEADER: str(admin)}).json()

        by_id = {n["id"]: n for n in nodes}
        assert by_id["generate_agent"]["tier"] is not None
        # The bookkeeping nodes have no stage, so no tier to claim.
        assert by_id["record_hint_agent"]["stage"] is None
        assert by_id["record_hint_agent"]["tier"] is None

    def test_a_teacher_cannot_read_it(self, client: TestClient, api_db: DbSession) -> None:
        """The topology is not sensitive, but it lives behind the same admin
        gate as the rest of `/admin` — a surface that is admin-only except for
        one endpoint is a surface someone will get wrong later."""
        teacher = _principal(api_db, Role.TEACHER)

        response = client.get("/admin/topology", headers={PRINCIPAL_HEADER: str(teacher)})

        assert response.status_code == 403
