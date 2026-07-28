"""Runtime enforcement of the append-only guarantee (§5).

§5 says every log-bearing table is append-only, because that is what makes both
teacher audit and later quality measurement possible. A comment saying so does
not survive contact with a developer in a hurry, so this makes the database
session refuse the write.

Two paths have to be closed, because closing only the first leaves an obvious
hole:

1. ORM mutation — loading a row, changing an attribute, flushing.
2. Bulk statements — ``session.execute(update(HintLogRow)...)``, which never
   loads an object and so never appears in ``session.dirty``.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import ORMExecuteState, Session


class AppendOnlyError(RuntimeError):
    """Raised when something tries to update or delete an append-only record."""


class AppendOnly:
    """Marker mixin. Inheriting from this makes a table's rows immutable."""

    __slots__ = ()


def _describe(obj: object) -> str:
    return type(obj).__name__


@event.listens_for(Session, "before_flush")
def _block_orm_mutation(session: Session, _flush_context: Any, _instances: Any) -> None:
    for obj in session.dirty:
        if isinstance(obj, AppendOnly) and session.is_modified(obj, include_collections=False):
            raise AppendOnlyError(
                f"{_describe(obj)} is append-only and cannot be modified; "
                f"append a new record instead"
            )
    for obj in session.deleted:
        if isinstance(obj, AppendOnly):
            raise AppendOnlyError(
                f"{_describe(obj)} is append-only and cannot be deleted; "
                f"log-bearing rows are retained for audit"
            )


@event.listens_for(Session, "do_orm_execute")
def _block_bulk_mutation(state: ORMExecuteState) -> None:
    if not (state.is_update or state.is_delete):
        return
    mapper = state.bind_mapper
    if mapper is not None and issubclass(mapper.class_, AppendOnly):
        verb = "updated" if state.is_update else "deleted"
        raise AppendOnlyError(f"{mapper.class_.__name__} is append-only and cannot be bulk-{verb}")
