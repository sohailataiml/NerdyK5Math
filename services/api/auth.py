"""Request authentication for the teacher console (P0.8).

**This is a pilot-grade stand-in and must not ship beyond the Phase 0 classroom
pilot.** It reads a principal ID from a header and trusts it. There is no
password, no session, no token signature — anyone who can reach the service can
claim to be any teacher.

That is a deliberate, bounded choice, and it is written here rather than
discovered later: Phase 0 runs in one classroom behind whatever network control
the school already has, and building real identity (OIDC against the district's
directory, most likely) before knowing whose directory it is would be guesswork.
What is *not* deferred is authorization — M0.9's scoping is fully enforced below,
so once identity is real the access rules do not change.

Replacing this means changing `current_scope` and nothing else.
"""

from __future__ import annotations

import uuid

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session as DbSession

from packages.auth import Scope, resolve_scope
from packages.domain.mapping import from_row
from packages.domain.models import Principal
from packages.domain.tables import PrincipalRow
from services.api.db import get_db

PRINCIPAL_HEADER = "x-principal-id"


def current_scope(
    principal_id: str | None = Header(default=None, alias=PRINCIPAL_HEADER),
    db: DbSession = Depends(get_db),
) -> Scope:
    """Resolve the caller and the students they may see."""
    if not principal_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"missing {PRINCIPAL_HEADER} header",
        )
    try:
        parsed = uuid.UUID(principal_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="not a principal id"
        ) from None

    row = db.get(PrincipalRow, parsed)
    if row is None:
        # Same shape as a wrong-but-real id: a distinguishable message would let
        # a caller enumerate which principal ids exist.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not a principal id")

    return resolve_scope(db, from_row(row, Principal))
