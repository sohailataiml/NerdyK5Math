"""Translation between §5 entities and their table rows.

Driven by SQLAlchemy column metadata rather than per-entity hand-written
mappers, so adding a field to an entity does not require remembering to update
a mapping function — a mismatch surfaces as a failing round-trip test instead of
as a column silently never written.

**Timestamp convention:** all datetimes are UTC. Columns are declared
``timezone=True``, but SQLite (used for fast tests) drops ``tzinfo`` on the way
out, so reads re-attach UTC rather than returning a naive datetime that would
compare unequal to what was written.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from pydantic import BaseModel
from sqlalchemy import DateTime, Uuid, inspect

from packages.domain.tables import Base


def _to_column(column_type: Any, value: Any) -> Any:
    """JSON-native value (from ``model_dump(mode="json")``) -> column value."""
    if value is None:
        return None
    if isinstance(column_type, Uuid):
        return uuid.UUID(value) if isinstance(value, str) else value
    if isinstance(column_type, DateTime):
        return dt.datetime.fromisoformat(value) if isinstance(value, str) else value
    return value


def _from_column(column_type: Any, value: Any) -> Any:
    if value is None:
        return None
    is_naive_datetime = isinstance(value, dt.datetime) and value.tzinfo is None
    if isinstance(column_type, DateTime) and is_naive_datetime:
        return value.replace(tzinfo=dt.UTC)
    return value


def to_row[TRow: Base](entity: BaseModel, row_cls: type[TRow]) -> TRow:
    """Build an unpersisted row from an entity.

    Dumps in JSON mode first so nested models and UUID lists land in JSON columns
    as serializable values, then converts back only the fields whose columns are
    genuinely typed (UUID, datetime).
    """
    columns = inspect(row_cls).columns
    data = entity.model_dump(mode="json")
    missing = set(data) - set(columns.keys())
    if missing:
        raise ValueError(
            f"{type(entity).__name__} has fields with no column on "
            f"{row_cls.__name__}: {sorted(missing)}"
        )
    kwargs = {name: _to_column(columns[name].type, value) for name, value in data.items()}
    return row_cls(**kwargs)


def from_row[TEntity: BaseModel](row: Base, entity_cls: type[TEntity]) -> TEntity:
    """Rebuild a validated entity from a row, re-running domain validation."""
    columns = inspect(type(row)).columns
    data = {name: _from_column(col.type, getattr(row, name)) for name, col in columns.items()}
    return entity_cls.model_validate(data)
