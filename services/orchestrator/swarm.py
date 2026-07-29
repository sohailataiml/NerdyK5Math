"""The §4 state machine, as a LangGraph multi-agent swarm.

    AwaitingAnswer -> Diagnosing -> RetrievingCurriculum -> GeneratingHint
    -> LeakChecking -> AwaitingStudentRetry | Escalated

(Grading happens on the student's *next* submission — a separate HTTP
request — so it stays outside this graph; `graph.py` drives it directly.)

Each stage is a node that, once it has done its work, hands control to the
next node itself via `Command(goto=..., update=...)` — LangGraph's low-level
handoff primitive, the same one `langgraph_swarm`'s chat-agent wrapper is
built on. There is no central node choosing "what runs next"; every node
owns its own routing decision. That is what makes this a swarm rather than a
supervisor calling stages in a list, and it is why this module replaces the
hand-written loop that used to live in `graph.py`.

One thing does **not** change with the engine: every handoff decision here is
still a deterministic function of stage output, exactly as it was in the old
loop — never a free choice an LLM makes at runtime. `langgraph_swarm`'s usual
shape lets a tool-calling agent decide for itself when to hand off; that is
the wrong shape for this pipeline; §3.3 is explicit that skipping the
leak-check is not a decision available to be made per swarm invocation.
Where a stage calls a model (diagnose, generate, leak-check), the model
produces *content* — a tag, a hint, a verdict — never the handoff. Wiring an
LLM's tool call into `goto` is exactly the failure mode the graph exists to
rule out, no matter which library draws the edges.

Two rules this graph enforces that no single node can:

- **A hint reaches a child only after `leakcheck_agent` passes it.** §3.3
  makes that guardrail blocking, and `generate_agent` has no edge that skips
  it — every generated candidate, and every substituted template, is routed
  through `leakcheck_agent` before it can reach `record_hint_agent`.
- **Never a third free generation.** `generate_agent` forces a template once
  `generation > MAX_GENERATION_ATTEMPTS`, and `leakcheck_agent` sends a
  rejected *template* straight to `escalate_agent` rather than looping — a
  template is deterministic, so retrying it would regenerate the identical
  rejection. The bound is structural: no path through this graph can revisit
  `generate_agent` more than `MAX_GENERATION_ATTEMPTS + 1` times.
"""

from __future__ import annotations

import datetime as dt
import typing
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, TypedDict
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from packages.domain.enums import HintSource, PipelineStage, SessionState
from packages.domain.models import HintLog, ShadowCandidate
from services.orchestrator.rollout import RolloutDecision, decide
from services.orchestrator.stages import diagnose, generate, leakcheck, retrieve, safety
from services.orchestrator.state import PipelineDeps, Problem

MAX_GENERATION_ATTEMPTS = 2
"""§3.3: regenerate once with a stricter prompt, then fall back to a template."""


@dataclass(frozen=True)
class _ShadowOutcome:
    """What the model produced in shadow, held until the shown hint is known.

    `ShadowCandidate` is append-only, so the row cannot be written when the model
    replies and patched once the template renders — and a rating without both
    hints is a teacher guessing. So the outcome waits here.
    """

    generated_text: str
    leak_passed: bool
    leak_checker_version: str
    leak_reason: str | None
    llm_call_id: UUID | None


@dataclass(frozen=True)
class AttemptResult:
    """What the child gets, and everything needed to explain why."""

    hint_text: str | None
    hint_level: int
    hint_source: HintSource | None
    diagnosis: diagnose.Diagnosis
    retrieval: retrieve.Retrieval
    leak_check: leakcheck.LeakCheck | None
    safety_screen: safety.SafetyScreen | None = None
    leak_rejections: int = 0
    """How many candidates the guardrail actually turned down.

    Counted rather than inferred from `hint_source`, because a template hint has
    three quite different causes — shadow mode by design, a provider outage, and
    the leak check firing — and only the third is a guardrail event. §3.6's
    routing needs to tell them apart, and a proxy cannot.
    """

    escalated: bool = False
    escalation_reason: str | None = None
    degraded_stages: tuple[str, ...] = ()
    shadow_ran: bool = False

    rollout: RolloutDecision | None = None
    """The P1.1 cohort decision for this session, when a rollout is configured.

    Reported rather than inferred from `hint_source`, for the same reason
    `leak_rejections` is counted: a template hint now has four causes — shadow
    mode, a provider outage, the leak check firing, and this session sitting
    outside the rollout — and only a recorded decision tells them apart. During a
    staged rollout the fourth is the *majority* of sessions, so a dashboard that
    cannot see it reads a working 5% rollout as a 95% failure rate.
    """

    @property
    def ran_degraded(self) -> bool:
        return bool(self.degraded_stages)


class SwarmState(TypedDict, total=False):
    """State handed between agents. Not persisted — one `run_attempt` call
    builds a fresh state, runs it to completion, and discards it; §5's audit
    trail is `deps.recorder`, not this graph's checkpoint.
    """

    deps: PipelineDeps
    session_id: UUID
    problem: Problem
    student_answer: str
    hint_level: int
    attempt: int
    student_id: UUID | None
    degraded: list[str]

    safety_screen: safety.SafetyScreen
    diagnosis: diagnose.Diagnosis
    retrieval: retrieve.Retrieval
    rollout: RolloutDecision | None
    withheld_by_rollout: bool
    shadow_ran: bool
    shadow_outcome: _ShadowOutcome | None

    generation: int
    leak_rejections: int
    candidate: generate.HintCandidate | None
    check: leakcheck.LeakCheck | None
    llm_call_id: UUID | None

    result: AttemptResult


def _template_reason(*, shadow_ran: bool, rollout: RolloutDecision | None) -> str:
    """Name *why* a template was served, for the audit record.

    Four causes produce the same hint text and must not read the same in a
    timeline: shadow mode, the rollout withholding, leak-check failures, and a
    provider outage (which `generate` names for itself). Reading one as another
    misstates what happened in a child's session — and during a staged rollout it
    would misstate the majority of them.
    """
    if shadow_ran:
        return "shadow mode: generated hints are recorded, not shown"
    if rollout is not None:
        return f"rollout: {rollout.reason}"
    return "template forced after leak-check failures"


def _run_shadow(
    deps: PipelineDeps,
    *,
    session_id: UUID,
    problem: Problem,
    tag: str,
    strategy: str,
    student_answer: str,
    level: int,
    attempt: int,
) -> tuple[bool, _ShadowOutcome | None]:
    """Run the model without showing it (P0.4).

    Returns `(shadow_ran, outcome)`. The outcome is deliberately *not* recorded
    here — see `_record_shadow`.
    """
    if not deps.shadow_mode or deps.llm is None:
        return False, None

    produced = generate.run(
        deps,
        session_id=session_id,
        problem=problem,
        tag=tag,
        strategy=strategy,
        student_answer=student_answer,
        level=level,
        attempt=attempt,
    )
    shadow = produced.value
    if shadow is None or shadow.source is not HintSource.GENERATED:
        return True, None

    checked = leakcheck.run(deps, session_id=session_id, problem=problem, hint_text=shadow.text)
    deps.recorder.stage_completed(
        PipelineStage.GENERATE_HINT,
        llm_call_id=produced.llm_call_id,
        shadow=True,
        leak_passed=checked.value.passed,
    )
    return True, _ShadowOutcome(
        generated_text=shadow.text,
        leak_passed=checked.value.passed,
        leak_checker_version=checked.value.checker_version,
        leak_reason=checked.value.reason,
        llm_call_id=produced.llm_call_id,
    )


def _record_hint(
    deps: PipelineDeps,
    *,
    session_id: UUID,
    attempt: int,
    candidate: generate.HintCandidate,
    check: leakcheck.LeakCheck,
    llm_call_id: UUID | None,
) -> None:
    """Persist what the child actually read (§5)."""
    if deps.hint_sink is None:
        return
    deps.hint_sink.record(
        HintLog(
            session_id=session_id,
            attempt_number=attempt,
            misconception_tag_id=None,
            curriculum_node_id=None,
            hint_text=candidate.text,
            hint_level=candidate.level,
            source=candidate.source,
            leak_check_passed=check.passed,
            leak_checker_version=check.checker_version,
            llm_call_id=llm_call_id,
        )
    )


def _record_shadow(
    deps: PipelineDeps,
    outcome: _ShadowOutcome | None,
    *,
    session_id: UUID,
    tag: str,
    level: int,
    attempt: int,
    shown_text: str | None,
) -> None:
    """Write the candidate now that both hints are known (P0.8)."""
    if outcome is None or deps.shadow_sink is None:
        return
    deps.shadow_sink.record(
        ShadowCandidate(
            session_id=session_id,
            attempt_number=attempt,
            hint_level=level,
            generated_text=outcome.generated_text,
            shown_text=shown_text,
            misconception_tag=tag,
            prompt_version=generate.PROMPT_VERSION,
            leak_check_passed=outcome.leak_passed,
            leak_checker_version=outcome.leak_checker_version,
            leak_reason=outcome.leak_reason,
            llm_call_id=outcome.llm_call_id,
            created_at=dt.datetime.now(dt.UTC),
        )
    )


# ---------------------------------------------------------------------------
# Agents. Each owns its own handoff — see the module docstring for why that
# handoff is always a deterministic function of stage output.
# ---------------------------------------------------------------------------


def _safety_agent(state: SwarmState) -> Command[Literal["diagnose_agent"]]:
    deps = state["deps"]
    screened = safety.run(
        deps,
        session_id=state["session_id"],
        student_id=state["student_id"],
        problem=state["problem"],
        student_answer=state["student_answer"],
        attempt=state["attempt"],
    )
    degraded = list(state["degraded"])
    if screened.used_fallback:
        degraded.append("safety_screen")

    deps.recorder.state_changed(frm=SessionState.AWAITING_ANSWER, to=SessionState.DIAGNOSING)
    return Command(
        goto="diagnose_agent",
        update={"safety_screen": screened.value, "degraded": degraded},
    )


def _diagnose_agent(state: SwarmState) -> Command[Literal["retrieve_agent"]]:
    deps = state["deps"]
    diagnosed = diagnose.run(
        deps,
        session_id=state["session_id"],
        problem=state["problem"],
        student_answer=state["student_answer"],
    )
    degraded = list(state["degraded"])
    if diagnosed.used_fallback:
        degraded.append("diagnose")

    deps.recorder.state_changed(frm=SessionState.DIAGNOSING, to=SessionState.RETRIEVING_CURRICULUM)
    return Command(
        goto="retrieve_agent",
        update={"diagnosis": diagnosed.value, "degraded": degraded},
    )


def _retrieve_agent(state: SwarmState) -> Command[Literal["shadow_agent"]]:
    deps = state["deps"]
    retrieved = retrieve.run(deps, tag=state["diagnosis"].tag, problem=state["problem"])
    degraded = list(state["degraded"])
    if retrieved.used_fallback:
        degraded.append("retrieve")

    deps.recorder.state_changed(
        frm=SessionState.RETRIEVING_CURRICULUM, to=SessionState.GENERATING_HINT
    )

    # P1.1: read the rollout cohort once per attempt, here, so every hint level
    # and generation attempt below resolves the same way.
    rollout = (
        decide(deps.rollout.current(), session_id=state["session_id"]) if deps.rollout else None
    )
    withheld_by_rollout = rollout is not None and not rollout.serve_generated

    return Command(
        goto="shadow_agent",
        update={
            "retrieval": retrieved.value,
            "degraded": degraded,
            "rollout": rollout,
            "withheld_by_rollout": withheld_by_rollout,
        },
    )


def _shadow_agent(state: SwarmState) -> Command[Literal["generate_agent"]]:
    deps = state["deps"]
    shadow_ran, shadow_outcome = _run_shadow(
        deps,
        session_id=state["session_id"],
        problem=state["problem"],
        tag=state["diagnosis"].tag,
        strategy=state["retrieval"].primary,
        student_answer=state["student_answer"],
        level=state["hint_level"],
        attempt=state["attempt"],
    )
    return Command(
        goto="generate_agent",
        update={
            "shadow_ran": shadow_ran,
            "shadow_outcome": shadow_outcome,
            "generation": 1,
            "leak_rejections": 0,
        },
    )


def _generate_agent(state: SwarmState) -> Command[Literal["leakcheck_agent", "escalate_agent"]]:
    deps = state["deps"]
    generation = state["generation"]
    shadow_ran = state["shadow_ran"]
    withheld_by_rollout = state["withheld_by_rollout"]

    # In shadow mode the served hint is always the template — the model already
    # ran, in `_shadow_agent`, and its output is evidence, not something a child
    # sees. Outside the rollout cohort it is also always the template, and no
    # model runs at all: a session that cannot be shown generated text should
    # not be billed for generating it (P1.10). Past `MAX_GENERATION_ATTEMPTS`,
    # force a template rather than a third free generation.
    force_template = shadow_ran or withheld_by_rollout or generation > MAX_GENERATION_ATTEMPTS

    produced = generate.run(
        deps,
        session_id=state["session_id"],
        problem=state["problem"],
        tag=state["diagnosis"].tag,
        strategy=state["retrieval"].primary,
        student_answer=state["student_answer"],
        level=state["hint_level"],
        attempt=state["attempt"],
        force_template=force_template,
        template_reason=_template_reason(
            shadow_ran=shadow_ran, rollout=state["rollout"] if withheld_by_rollout else None
        ),
    )
    by_design = shadow_ran or withheld_by_rollout
    degraded = list(state["degraded"])
    if produced.used_fallback and not by_design and "generate" not in degraded:
        degraded.append("generate")

    candidate = produced.value
    if candidate is None:
        # Even the template would leak for this problem. No safe hint exists —
        # an escalation, not a retry.
        return Command(
            goto="escalate_agent",
            update={"candidate": None, "degraded": degraded},
        )

    deps.recorder.state_changed(frm=SessionState.GENERATING_HINT, to=SessionState.LEAK_CHECKING)
    return Command(
        goto="leakcheck_agent",
        update={"candidate": candidate, "degraded": degraded, "llm_call_id": produced.llm_call_id},
    )


def _leakcheck_agent(
    state: SwarmState,
) -> Command[Literal["record_hint_agent", "generate_agent", "escalate_agent"]]:
    deps = state["deps"]
    candidate = state["candidate"]
    assert candidate is not None  # only reached with a candidate to check

    checked = leakcheck.run(
        deps, session_id=state["session_id"], problem=state["problem"], hint_text=candidate.text
    )
    degraded = list(state["degraded"])
    if checked.used_fallback and "leakcheck" not in degraded:
        degraded.append("leakcheck")

    check = checked.value
    leak_rejections = state["leak_rejections"]
    if not check.passed:
        leak_rejections += 1

    if not check.passed and candidate.source is HintSource.TEMPLATE_FALLBACK:
        # A template is deterministic: retrying would regenerate the identical
        # text and the identical rejection. Escalate rather than loop.
        return Command(
            goto="escalate_agent",
            update={
                "candidate": None,
                "check": check,
                "degraded": degraded,
                "leak_rejections": leak_rejections,
            },
        )

    if check.passed:
        return Command(
            goto="record_hint_agent",
            update={"check": check, "degraded": degraded, "leak_rejections": leak_rejections},
        )

    # Rejected, but the candidate was generated — hand back to `generate_agent`
    # for the next (stricter) attempt. `generation + 1` is what eventually
    # forces `force_template=True` there, bounding this handoff by construction.
    return Command(
        goto="generate_agent",
        update={
            "candidate": None,
            "check": check,
            "degraded": degraded,
            "leak_rejections": leak_rejections,
            "generation": state["generation"] + 1,
        },
    )


def _record_hint_agent(state: SwarmState) -> Command[Literal[END]]:  # type: ignore[valid-type]
    deps = state["deps"]
    candidate = state["candidate"]
    check = state["check"]
    assert candidate is not None and check is not None

    deps.recorder.hint_shown(
        hint_level=candidate.level,
        source=candidate.source.value,
        leak_checker_version=check.checker_version,
    )
    # §5's HintLog: the *text* the child read. The event above records that a
    # hint was shown, at what level and from what source — not what it said.
    _record_hint(
        deps,
        session_id=state["session_id"],
        attempt=state["attempt"],
        candidate=candidate,
        check=check,
        llm_call_id=state.get("llm_call_id"),
    )
    deps.recorder.state_changed(
        frm=SessionState.LEAK_CHECKING, to=SessionState.AWAITING_STUDENT_RETRY
    )
    _record_shadow(
        deps,
        state["shadow_outcome"],
        session_id=state["session_id"],
        tag=state["diagnosis"].tag,
        level=candidate.level,
        attempt=state["attempt"],
        shown_text=candidate.text,
    )

    result = AttemptResult(
        hint_text=candidate.text,
        hint_level=candidate.level,
        hint_source=candidate.source,
        diagnosis=state["diagnosis"],
        retrieval=state["retrieval"],
        leak_check=check,
        safety_screen=state["safety_screen"],
        leak_rejections=state["leak_rejections"],
        degraded_stages=tuple(state["degraded"]),
        shadow_ran=state["shadow_ran"],
        rollout=state["rollout"],
    )
    return Command(goto=END, update={"result": result})


def _escalate_agent(state: SwarmState) -> Command[Literal[END]]:  # type: ignore[valid-type]
    deps = state["deps"]
    check = state.get("check")
    reason = (check.reason if check else None) or "no hint could be cleared for display"

    deps.recorder.escalated(reason=reason)
    deps.recorder.state_changed(frm=SessionState.LEAK_CHECKING, to=SessionState.ESCALATED)
    _record_shadow(
        deps,
        state["shadow_outcome"],
        session_id=state["session_id"],
        tag=state["diagnosis"].tag,
        level=state["hint_level"],
        attempt=state["attempt"],
        shown_text=None,
    )

    result = AttemptResult(
        hint_text=None,
        hint_level=state["hint_level"],
        hint_source=None,
        diagnosis=state["diagnosis"],
        retrieval=state["retrieval"],
        leak_check=check,
        safety_screen=state["safety_screen"],
        leak_rejections=state["leak_rejections"],
        escalated=True,
        escalation_reason=reason,
        degraded_stages=tuple(state["degraded"]),
        shadow_ran=state["shadow_ran"],
        rollout=state["rollout"],
    )
    return Command(goto=END, update={"result": result})


ENTRY_NODE = "safety_agent"

AGENTS: dict[str, Callable[[SwarmState], Any]] = {
    "safety_agent": _safety_agent,
    "diagnose_agent": _diagnose_agent,
    "retrieve_agent": _retrieve_agent,
    "shadow_agent": _shadow_agent,
    "generate_agent": _generate_agent,
    "leakcheck_agent": _leakcheck_agent,
    "record_hint_agent": _record_hint_agent,
    "escalate_agent": _escalate_agent,
}
"""The swarm's nodes, in the order a run normally visits them.

A single mapping rather than a list of `add_node` calls, because `_build_swarm`
is no longer the only thing that needs to know the roster: `topology()` derives
the drawable graph from it, and a picture built from a second, hand-maintained
list is a picture that drifts from the code the first time a node is added.
"""

NODE_STAGE: dict[str, PipelineStage | None] = {
    "safety_agent": PipelineStage.SAFETY_SCREEN,
    "diagnose_agent": PipelineStage.DIAGNOSE,
    "retrieve_agent": PipelineStage.RERANK,
    "shadow_agent": PipelineStage.GENERATE_HINT,
    "generate_agent": PipelineStage.GENERATE_HINT,
    "leakcheck_agent": PipelineStage.LEAK_CHECK,
    # These two do bookkeeping at the graph's terminal edges — they write the
    # record and hand back a result. `None` is what tells a viewer not to look
    # for a stage run that was never going to exist.
    "record_hint_agent": None,
    "escalate_agent": None,
}


NODE_PURPOSE: dict[str, str] = {
    "safety_agent": (
        "Reads the child's own words for signs of distress, before any teaching "
        "happens — whether the maths was right has no bearing on whether a child "
        "needs an adult. A flag goes sideways to a person and never changes what "
        "the child sees, because a machine that visibly stops after a child types "
        "something frightening teaches them not to type it again."
    ),
    "diagnose_agent": (
        "Works out *which* mistake was made, not just that the answer was wrong — "
        "the whole pipeline downstream is aimed at a named misconception. A free "
        "arithmetic rule check runs first and the model is asked only when no rule "
        "fires; when neither is confident the answer is `unknown`, because a "
        "confident wrong diagnosis sends a child a hint for a problem they do not "
        "have."
    ),
    "retrieve_agent": (
        "Looks up the teacher-approved way to reteach that specific mistake. "
        "Generation is never allowed to invent a teaching strategy, so an empty "
        "result blocks it — this is the stage that stops the model making up "
        "curriculum."
    ),
    "shadow_agent": (
        "Phase 0 only. Runs the model and records its hint for a teacher to rate "
        "without showing it to anyone, so the system can be measured on real "
        "children's work before a word of it reaches a child. Off outside shadow "
        "mode, when it does nothing at all."
    ),
    "generate_agent": (
        "Phrases the retrieved strategy as a Socratic hint for this child, this "
        "problem, this attempt. It chooses the words, never the teaching move. "
        "After two rejected attempts it stops asking the model and renders a "
        "pre-approved template instead."
    ),
    "leakcheck_agent": (
        "Blocks any hint that gives the answer away — a normalised match first, "
        "then a model asked to judge. Nothing reaches a child without passing "
        "here, templates included, and it fails closed: no hint is better than a "
        "leaked one, because a leak cannot be taken back once a child has read it."
    ),
    "record_hint_agent": (
        "Writes down what the child was actually shown, so a teacher reviewing the "
        "session months later reads the same words the child did. It refuses to "
        "store anything the leak check did not clear."
    ),
    "escalate_agent": (
        "Ends the attempt with no hint and sends the session to a teacher. "
        "Deliberately not a failure state for the child: they are told an adult "
        "will look at it with them, and that sentence is only written when a "
        "queue row was really created."
    ),
}
"""One plain sentence per node, for the canvas and for anyone reading the graph
for the first time.

Prose, so unlike the edges it cannot be derived — but it lives beside the code it
describes rather than in a diagram somewhere else, and a test asserts every node
has one, so adding a node forces an explanation of why it exists.
"""


def handoffs(node: str) -> tuple[str, ...]:
    """Where `node` may hand control, read from its own return annotation.

    `Command[Literal["a", "b"]]` is not documentation — LangGraph reads the same
    annotation to validate the graph, so deriving the edges from it means a
    drawing of this swarm cannot disagree with the swarm. Adding a handoff
    target without updating the annotation is already a bug; this makes it a
    visible one.
    """
    annotation = typing.get_type_hints(AGENTS[node])["return"]
    command_args = typing.get_args(annotation)
    if not command_args:
        return ()
    return tuple(str(target) for target in typing.get_args(command_args[0]))


@dataclass(frozen=True)
class SwarmNode:
    """One node of the drawable graph."""

    id: str
    stage: PipelineStage | None
    entry: bool
    handoffs: tuple[str, ...]
    purpose: str


def topology() -> tuple[SwarmNode, ...]:
    """The swarm as drawable nodes and edges, derived rather than declared."""
    return tuple(
        SwarmNode(
            id=node,
            stage=NODE_STAGE[node],
            entry=node == ENTRY_NODE,
            handoffs=handoffs(node),
            purpose=NODE_PURPOSE[node],
        )
        for node in AGENTS
    )


def _build_swarm() -> CompiledStateGraph[SwarmState, None, SwarmState, SwarmState]:
    builder = StateGraph(SwarmState)
    for name, agent in AGENTS.items():
        # `add_node` is heavily overloaded on the action's shape, and mypy cannot
        # resolve those overloads against a callable read from a dict rather than
        # named directly. Registering from the roster is worth one narrow ignore:
        # the alternative is a second, hand-maintained list of nodes that drifts
        # from this one the first time a node is added.
        builder.add_node(name, agent)  # type: ignore[call-overload]
    # The only static edge: everything past the entry point is a handoff a node
    # decides for itself (see the module docstring).
    builder.add_edge(START, ENTRY_NODE)
    return builder.compile()


SWARM = _build_swarm()
"""Compiled once at import time. No checkpointer: a `run_attempt` call builds a
fresh `SwarmState`, runs it to completion synchronously, and discards it —
resuming a session across the student's next HTTP request is the caller's job
(`graph.check_answer` / `graph.grade_answer`), not this graph's."""


def run(
    deps: PipelineDeps,
    *,
    session_id: UUID,
    problem: Problem,
    student_answer: str,
    hint_level: int,
    attempt: int,
    student_id: UUID | None,
) -> AttemptResult:
    """Invoke the swarm for one attempt and return the terminal `AttemptResult`."""
    final_state = SWARM.invoke(
        {
            "deps": deps,
            "session_id": session_id,
            "problem": problem,
            "student_answer": student_answer,
            "hint_level": hint_level,
            "attempt": attempt,
            "student_id": student_id,
            "degraded": [],
        }
    )
    result = final_state["result"]
    assert isinstance(result, AttemptResult)
    return result
