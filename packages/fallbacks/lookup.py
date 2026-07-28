"""Keyed curriculum lookup (§3.2 fallback, M0.12).

The degradation path for retrieval. §3.2 uses embedding search plus an LLM
reranker; when the provider is unavailable this replaces it with the exact-match
join the prior architecture revision used: the diagnoser emits a discrete tag, so
`(tag, grade_band)` maps directly to a curriculum node and its approved
strategies.

Strictly less capable, and knowingly so — it cannot rerank by attempt history and
it returns nothing for a tag nobody mapped. What it does guarantee is that a
retrieval failure degrades to teacher-approved content rather than to nothing,
which is the property §4 asks for.

§3.2's absolute rule still holds here: generation never proceeds on empty
retrieval, because that is the condition under which models invent curriculum. A
miss returns the general-purpose strategy instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from packages.domain.enums import GradeBand
from packages.domain.tables import CurriculumNodeRow, MisconceptionTagRow

GENERAL_STRATEGY = "Let's look at this together — walk me through how you worked it out."
"""§3.2's fallback strategy, used when a tag has no mapped node.

Deliberately a real teaching move rather than an apology. A child sees this
because the system does not recognise their error, and the honest response to
that is to ask them.
"""


@dataclass(frozen=True)
class LookupResult:
    node_id: UUID | None
    strategies: tuple[str, ...]
    used_fallback: bool
    reason: str | None = None

    @property
    def primary_strategy(self) -> str:
        return self.strategies[0] if self.strategies else GENERAL_STRATEGY


def lookup(db: DbSession, *, tag_label: str, grade_band: GradeBand) -> LookupResult:
    """Find approved remediation strategies for a diagnosed tag."""
    tag = db.execute(
        select(MisconceptionTagRow).where(MisconceptionTagRow.label == tag_label)
    ).scalar_one_or_none()
    if tag is None:
        return LookupResult(
            node_id=None,
            strategies=(GENERAL_STRATEGY,),
            used_fallback=True,
            reason=f"no misconception tag named {tag_label!r}",
        )

    # Nodes list the tags they address, and are grade-band scoped: the same error
    # is remediated differently for a 6-year-old and a 12-year-old (§1.5).
    candidates = (
        db.execute(select(CurriculumNodeRow).where(CurriculumNodeRow.grade_band == grade_band))
        .scalars()
        .all()
    )
    for node in candidates:
        if node.remediation_strategies:
            return LookupResult(
                node_id=node.id,
                strategies=tuple(node.remediation_strategies),
                used_fallback=False,
            )

    return LookupResult(
        node_id=None,
        strategies=(GENERAL_STRATEGY,),
        used_fallback=True,
        reason=f"no curriculum node for {tag_label!r} in grade band {grade_band.value}",
    )
