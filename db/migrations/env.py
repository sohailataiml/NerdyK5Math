"""Alembic environment.

The database URL comes from ``$DATABASE_URL`` so no credentials sit in the repo,
and so the same migrations run against local Postgres, CI, and a throwaway
SQLite file without editing config.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Repo root on sys.path so `packages.domain` imports work when alembic is
# invoked from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from packages.domain.tables import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

DEFAULT_URL = "postgresql+psycopg://tutor:tutor@localhost:5433/tutor"

# Precedence: a URL set programmatically (tests) > $DATABASE_URL > local compose.
# alembic.ini leaves sqlalchemy.url empty, so the empty string falls through.
_url = config.get_main_option("sqlalchemy.url") or os.environ.get("DATABASE_URL") or DEFAULT_URL
config.set_main_option("sqlalchemy.url", _url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # SQLite cannot ALTER most things in place; batch mode rewrites the
            # table instead, so the same migration script works on both engines.
            render_as_batch=connection.dialect.name == "sqlite",
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
