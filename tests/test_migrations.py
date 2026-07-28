"""M0.3 — migrations run from empty, reverse cleanly, and match the models.

The drift test is the important one. A migration that has quietly diverged from
``Base.metadata`` produces a database where the ORM half-works: reads succeed
until they touch the column nobody migrated. Comparing the two directly turns
that into a red test at the moment the model changes.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url

from packages.curriculum.seed import seed
from packages.domain.tables import Base

REPO_ROOT = Path(__file__).resolve().parents[1]


def _alembic_config(url: str) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "db" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture
def sqlite_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'migrations.db'}"


def test_upgrade_head_from_empty_database(sqlite_url: str) -> None:
    command.upgrade(_alembic_config(sqlite_url), "head")

    engine = create_engine(sqlite_url)
    tables = set(inspect(engine).get_table_names())
    engine.dispose()

    assert "alembic_version" in tables
    assert set(Base.metadata.tables) <= tables


def test_downgrade_removes_every_table(sqlite_url: str) -> None:
    cfg = _alembic_config(sqlite_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")

    engine = create_engine(sqlite_url)
    remaining = set(inspect(engine).get_table_names()) - {"alembic_version"}
    engine.dispose()

    assert remaining == set()


def test_migration_schema_matches_the_models(sqlite_url: str) -> None:
    """Catch model/migration drift: same tables, same columns, both directions."""
    command.upgrade(_alembic_config(sqlite_url), "head")

    engine = create_engine(sqlite_url)
    inspector = inspect(engine)
    migrated = {
        name: {col["name"] for col in inspector.get_columns(name)}
        for name in inspector.get_table_names()
        if name != "alembic_version"
    }
    engine.dispose()

    declared = {name: set(table.columns.keys()) for name, table in Base.metadata.tables.items()}

    assert migrated.keys() == declared.keys(), "tables differ between migration and models"
    for table_name, columns in declared.items():
        assert migrated[table_name] == columns, f"columns differ on {table_name}"


def test_seed_loads_the_fixture_kb(sqlite_url: str) -> None:
    """M0.3 exit criterion: a fixture KB loads against a migrated database."""
    from sqlalchemy.orm import Session

    from packages.domain import tables as t

    command.upgrade(_alembic_config(sqlite_url), "head")
    engine = create_engine(sqlite_url)
    with Session(engine) as db:
        seed(db)
        assert db.query(t.CurriculumNodeRow).count() == 3
        assert db.query(t.MisconceptionTagRow).count() == 3
        assert db.query(t.ProblemRow).count() == 2

        seed(db)  # idempotent
        assert db.query(t.CurriculumNodeRow).count() == 3
    engine.dispose()


# ---------------------------------------------------------------------------
# Postgres-only. Everything above proves the schema is coherent; only these
# prove the pgvector extension and the vector column actually work, because
# SQLite silently accepts the JSON variant instead.
# ---------------------------------------------------------------------------

POSTGRES_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://tutor:tutor@localhost:5433/tutor_test"
)


def _assert_disposable(url: str) -> None:
    """Refuse to run destructive tests against a database that isn't a test one.

    These tests call `downgrade base`, which drops every table. Pointed at the
    development database that silently destroys whatever you were working with;
    pointed at anything real it would be considerably worse. The name check is
    crude, and that is the point — it cannot be satisfied by accident.
    """
    database = make_url(url).database or ""
    if not database.endswith("_test"):
        pytest.fail(
            f"refusing to run destructive migration tests against {database!r}. "
            f"TEST_DATABASE_URL must name a database ending in '_test' "
            f"(the compose file creates 'tutor_test' for this)."
        )


@pytest.mark.integration
def test_upgrade_head_on_postgres_creates_pgvector() -> None:
    from sqlalchemy import text

    _assert_disposable(POSTGRES_URL)

    cfg = _alembic_config(POSTGRES_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")

    engine = create_engine(POSTGRES_URL)
    with engine.connect() as conn:
        has_vector = conn.execute(
            text("SELECT count(*) FROM pg_extension WHERE extname = 'vector'")
        ).scalar_one()
        column_type = conn.execute(
            text(
                "SELECT udt_name FROM information_schema.columns "
                "WHERE table_name = 'curriculum_node' AND column_name = 'embedding'"
            )
        ).scalar_one()
    engine.dispose()

    assert has_vector == 1
    assert column_type == "vector"


def test_embedding_column_keeps_pgvector_operators() -> None:
    """Regression guard on the variant ordering.

    ``Vector(...).with_variant(JSON(), "sqlite")`` and
    ``JSON().with_variant(Vector(...), "postgresql")`` emit identical DDL, but
    SQLAlchemy takes the comparator from the *base* type. Declared JSON-first,
    these operators vanish and every §3.2 retrieval query has to drop to raw SQL
    — a failure that no schema test would catch, because the column is fine.
    """
    from packages.domain.tables import CurriculumNodeRow

    column = CurriculumNodeRow.embedding
    assert hasattr(column, "cosine_distance")
    assert hasattr(column, "l2_distance")
    assert hasattr(column, "max_inner_product")


@pytest.mark.integration
def test_similarity_search_returns_the_nearest_node() -> None:
    """The actual §3.2 capability: rank curriculum nodes by embedding distance.

    A `vector`-typed column proves the extension loaded; only a query proves
    retrieval will work.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from packages.curriculum.seed import NODE_SUB_WITHIN_20
    from packages.domain import tables as t
    from packages.domain.tables import EMBEDDING_DIM

    _assert_disposable(POSTGRES_URL)

    cfg = _alembic_config(POSTGRES_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")

    engine = create_engine(POSTGRES_URL)
    with Session(engine) as db:
        seed(db)

        # Three distinguishable unit vectors, one per node.
        def unit(axis: int) -> list[float]:
            vec = [0.0] * EMBEDDING_DIM
            vec[axis] = 1.0
            return vec

        nodes = db.query(t.CurriculumNodeRow).order_by(t.CurriculumNodeRow.standard_code).all()
        for axis, node in enumerate(nodes):
            node.embedding = unit(axis)
            node.embedding_version = 1
        db.commit()

        target = db.get(t.CurriculumNodeRow, NODE_SUB_WITHIN_20)
        assert target is not None
        probe = list(target.embedding or [])

        nearest = db.execute(
            select(t.CurriculumNodeRow)
            .where(t.CurriculumNodeRow.embedding.is_not(None))
            .order_by(t.CurriculumNodeRow.embedding.cosine_distance(probe))
            .limit(1)
        ).scalar_one()

        assert nearest.id == NODE_SUB_WITHIN_20
    engine.dispose()


@pytest.mark.integration
def test_seed_loads_against_postgres() -> None:
    from sqlalchemy.orm import Session

    from packages.domain import tables as t

    _assert_disposable(POSTGRES_URL)

    cfg = _alembic_config(POSTGRES_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")

    engine = create_engine(POSTGRES_URL)
    with Session(engine) as db:
        seed(db)
        assert db.query(t.CurriculumNodeRow).count() == 3
    engine.dispose()
