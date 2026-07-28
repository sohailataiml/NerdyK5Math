"""Phase 0 readiness (Implementation-Plan.md Phase 0 exit criteria).

Everything built so far *produces* evidence — shadow candidates, teacher ratings,
the event log, the ledger. Nothing until now *read* that evidence against the
gates that decide whether generated text may reach a child. This does.

The gates are the plan's, restated as computations rather than prose:

1. >= 200 teacher-reviewed sessions from a real pilot classroom
2. Diagnoser calibration: predictions at >= 0.8 confidence are >= 90% confirmed
3. Leak-checker: zero confirmed leaks; 100% on the adversarial corpus
4. Teachers rate the generated hint at least as good as the template in >= 70%
5. Every stage's degradation path exercised under a simulated outage
6. Compliance sign-off (M0.11)

**A gate with no data reads INSUFFICIENT, never PASS.** That is the single most
important property here, and the same failure the eval harness guards against: a
report that says "all gates green" over an empty database would be the most
expensive bug in this repo, because the decision it unblocks is showing
model-generated text to children.

Gate 6 is not computable and does not pretend to be. It is a human sign-off, and
the report says so rather than quietly omitting it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from packages.domain.enums import EventType
from packages.domain.tables import (
    AttemptRow,
    LLMCallRow,
    PipelineEventRow,
    SessionRow,
    ShadowCandidateRow,
    ShadowRatingRow,
)

MIN_REVIEWED_SESSIONS = 200
MIN_HIGH_CONFIDENCE_PRECISION = 0.90
MIN_SHADOW_WIN_RATE = 0.70
REQUIRED_DEGRADED_STAGES = frozenset(
    {"diagnose", "rerank", "generate_hint", "leak_check", "safety_screen"}
)
"""Every stage with a degradation path, including the §7 welfare screen (P1.8).

`safety_screen` is here because a pilot running with its classifier down is a
pilot screening on patterns alone, and criterion 5 asks whether that has been
seen rather than assumed. Adding a stage to the pipeline without adding it here
would let the gate keep passing while covering less than it claims.
"""

MIN_REVIEWED_FOR_A_RATE = 30
"""Below this, a proportion is noise and an absence is nobody having looked.

Well under the 200-session bar, so it never becomes the operative limit — it only
stops a handful of ratings from rendering as a conclusion either way.
"""


class GateStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INSUFFICIENT = "INSUFFICIENT DATA"
    MANUAL = "NEEDS A HUMAN"


@dataclass(frozen=True)
class Gate:
    name: str
    status: GateStatus
    detail: str

    @property
    def blocks_exit(self) -> bool:
        return self.status is not GateStatus.PASS


@dataclass(frozen=True)
class Phase0Report:
    gates: tuple[Gate, ...]
    notes: tuple[str, ...] = ()
    """Context that informs the decision without gating it — pilot cost, volumes.

    Kept apart from `gates` on purpose: a number that cannot fail does not belong
    in a list whose whole meaning is pass or fail, and mixing them makes a report
    look greener than it is.
    """

    @property
    def ready(self) -> bool:
        return all(not g.blocks_exit for g in self.gates)

    def render(self) -> str:
        width = max(len(g.name) for g in self.gates)
        lines = ["", "=== Phase 0 readiness ===", ""]
        for gate in self.gates:
            lines.append(f"  [{gate.status.value:<17}] {gate.name:<{width}}  {gate.detail}")
        if self.notes:
            lines.append("")
            for note in self.notes:
                lines.append(f"  · {note}")
        lines.append("")
        if self.ready:
            lines.append("  All gates met. Generated hints may be shown to students.")
        else:
            blocking = [g.name for g in self.gates if g.blocks_exit]
            lines.append(
                f"  NOT READY — {len(blocking)} gate(s) outstanding: {', '.join(blocking)}"
            )
            lines.append("  Shadow mode stays on. Generated text does not reach a child.")
        return "\n".join(lines)


def _reviewed_sessions(db: DbSession) -> Gate:
    """Gate 1: enough rated sessions to say anything at all."""
    rated = db.execute(
        select(func.count(func.distinct(ShadowCandidateRow.session_id)))
        .select_from(ShadowRatingRow)
        .join(
            ShadowCandidateRow,
            ShadowCandidateRow.id == ShadowRatingRow.shadow_candidate_id,
        )
    ).scalar_one()

    if rated >= MIN_REVIEWED_SESSIONS:
        return Gate(
            "reviewed sessions",
            GateStatus.PASS,
            f"{rated} rated (need {MIN_REVIEWED_SESSIONS})",
        )
    return Gate(
        "reviewed sessions",
        GateStatus.INSUFFICIENT,
        f"{rated} rated, need {MIN_REVIEWED_SESSIONS} from a real classroom",
    )


def _shadow_win_rate(db: DbSession) -> Gate:
    """Gate 4: is generation actually beating the deterministic path?

    The question Phase 0 exists to answer. If the template is as good, the cost,
    latency, and leak risk of generation buy nothing.
    """
    total = db.execute(select(func.count()).select_from(ShadowRatingRow)).scalar_one()
    if total == 0:
        return Gate("generation beats template", GateStatus.INSUFFICIENT, "no ratings yet")

    better = db.execute(
        select(func.count())
        .select_from(ShadowRatingRow)
        .where(ShadowRatingRow.better_than_shown.is_(True))
    ).scalar_one()
    rate = better / total

    # A handful of ratings cannot establish a rate; saying 100% off three is
    # exactly the kind of number that gets quoted in a go/no-go meeting.
    if total < MIN_REVIEWED_FOR_A_RATE:
        return Gate(
            "generation beats template",
            GateStatus.INSUFFICIENT,
            f"{better}/{total} rated better ({rate:.0%}) — too few to conclude",
        )
    status = GateStatus.PASS if rate >= MIN_SHADOW_WIN_RATE else GateStatus.FAIL
    return Gate(
        "generation beats template",
        status,
        f"{better}/{total} rated better ({rate:.0%}, need {MIN_SHADOW_WIN_RATE:.0%})",
    )


def _leak_safety(db: DbSession) -> Gate:
    """Gate 3: no leak reached a child, and none was flagged by a teacher.

    Shadow mode makes the first half trivially true — nothing generated is shown
    — so what this really measures is the checker's performance on real model
    output, plus anything a teacher caught that the checker did not.

    A flag fails the gate immediately regardless of volume: one confirmed miss is
    a miss. But *zero* flags only means something once teachers have reviewed
    enough hints to have found one. Below that, "no leaks reported" is nobody
    having looked, and reporting it as PASS is precisely the failure this module
    exists to prevent — with the highest stakes of any gate here.
    """
    flagged = db.execute(
        select(func.count())
        .select_from(ShadowRatingRow)
        .where(ShadowRatingRow.would_leak.is_(True))
    ).scalar_one()
    checked = db.execute(select(func.count()).select_from(ShadowCandidateRow)).scalar_one()
    caught = db.execute(
        select(func.count())
        .select_from(ShadowCandidateRow)
        .where(ShadowCandidateRow.leak_check_passed.is_(False))
    ).scalar_one()
    reviewed = db.execute(select(func.count()).select_from(ShadowRatingRow)).scalar_one()

    if flagged:
        return Gate(
            "leak safety",
            GateStatus.FAIL,
            f"{flagged} hint(s) a teacher judged leaky that the checker passed — "
            f"add them to eval/adversarial/leak_corpus.jsonl before proceeding",
        )
    if checked == 0:
        return Gate("leak safety", GateStatus.INSUFFICIENT, "no generated hints checked yet")
    if reviewed < MIN_REVIEWED_FOR_A_RATE:
        return Gate(
            "leak safety",
            GateStatus.INSUFFICIENT,
            f"{checked} hint(s) checked but only {reviewed} teacher-reviewed — "
            f"no leak reported is nobody having looked",
        )
    return Gate(
        "leak safety",
        GateStatus.PASS,
        f"{caught}/{checked} caught by the checker, "
        f"0 missed by it across {reviewed} teacher reviews",
    )


def _degradation(db: DbSession) -> Gate:
    """Gate 5: every stage's fallback has actually run, not just been written.

    Read from the event log, not from the test suite, and the two say different
    things. Every fallback here is unit-tested; that proves the branch works when
    called. This proves it was *reached* by a real session — which `--offline`
    alone does not do, because a stage whose deterministic path succeeds first
    (a diagnosis rule matches, a curriculum node is mapped) never asks the model
    and so never falls back. Those stages need a drill with an input that misses.
    """
    rows = (
        db.execute(
            select(PipelineEventRow.stage).where(
                PipelineEventRow.event_type == EventType.FALLBACK_USED
            )
        )
        .scalars()
        .all()
    )
    exercised = {stage.value for stage in rows if stage is not None}
    missing = REQUIRED_DEGRADED_STAGES - exercised

    if not exercised:
        return Gate("degradation paths", GateStatus.INSUFFICIENT, "no fallback ever recorded")
    if missing:
        return Gate(
            "degradation paths",
            GateStatus.FAIL,
            f"never exercised by a real session: {', '.join(sorted(missing))} "
            f"— needs a drill whose input misses their deterministic path",
        )
    return Gate(
        "degradation paths",
        GateStatus.PASS,
        f"all {len(REQUIRED_DEGRADED_STAGES)} stages have fallen back at least once",
    )


def _calibration(db: DbSession) -> Gate:
    """Gate 2: is a stated confidence worth anything?

    Measured properly by the eval harness against teacher-confirmed labels
    (`eval.harness.cli run diagnosis --predictor llm`). This gate reports whether
    that measurement has been made on pilot data, rather than recomputing it here
    from a different source and risking two numbers that disagree.
    """
    corrections = db.execute(
        select(func.count())
        .select_from(ShadowRatingRow)
        .where(ShadowRatingRow.notes.like("%corrected tag:%"))
    ).scalar_one()
    total = db.execute(select(func.count()).select_from(ShadowRatingRow)).scalar_one()

    if total < MIN_REVIEWED_FOR_A_RATE:
        return Gate(
            "diagnoser calibration",
            GateStatus.INSUFFICIENT,
            f"{total} teacher-labelled example(s); run the eval suite once the pilot set exists",
        )
    agreement = 1 - (corrections / total)
    status = GateStatus.PASS if agreement >= MIN_HIGH_CONFIDENCE_PRECISION else GateStatus.FAIL
    return Gate(
        "diagnoser calibration",
        status,
        f"teachers corrected {corrections}/{total} tags "
        f"({agreement:.0%} agreement, need {MIN_HIGH_CONFIDENCE_PRECISION:.0%})",
    )


def _compliance() -> Gate:
    """Gate 6 (M0.11). Not computable, and not silently omitted either."""
    return Gate(
        "compliance sign-off",
        GateStatus.MANUAL,
        "DPA, zero-retention config, and district data-flow review — no code can attest to this",
    )


def _notes(db: DbSession) -> tuple[str, ...]:
    """Context for the decision (§8), deliberately not gates."""
    total = db.execute(select(func.coalesce(func.sum(LLMCallRow.cost_usd), 0.0))).scalar_one()
    calls = db.execute(select(func.count()).select_from(LLMCallRow)).scalar_one()
    candidates = db.execute(select(func.count()).select_from(ShadowCandidateRow)).scalar_one()
    per_session = f"${total / candidates:.4f}" if candidates else "n/a"

    # Sessions nobody ever answered: a page opened and left. Reported rather than
    # swept, and deliberately not given a state transition — nothing performs
    # one, and a status no code sets is a status that lies as soon as someone
    # trusts it. The gates above count ratings, so these do not distort them
    # today; they would distort any later "sessions started" denominator, which
    # is exactly why the number is visible here instead of inferred.
    started = db.execute(select(func.count()).select_from(SessionRow)).scalar_one()
    answered = db.execute(select(func.count(func.distinct(AttemptRow.session_id)))).scalar_one()
    unanswered = started - answered

    notes = [
        f"pilot cost to date: ${total:.4f} across {calls} model call(s)",
        f"shadow candidates recorded: {candidates} ({per_session} per candidate)",
    ]
    if unanswered:
        notes.append(
            f"{unanswered} of {started} session(s) were opened and never answered — "
            f"exclude these from any per-session rate"
        )
    return tuple(notes)


def build_report(db: DbSession) -> Phase0Report:
    return Phase0Report(
        gates=(
            _reviewed_sessions(db),
            _calibration(db),
            _leak_safety(db),
            _shadow_win_rate(db),
            _degradation(db),
            _compliance(),
        ),
        notes=_notes(db),
    )
