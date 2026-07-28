"""Composition root for the pipeline behind the student API (P0.11).

`scripts/run_session.py` wires this up for a one-off demo. A served request needs
the same wiring per request, and it needs the *safe* defaults rather than the
demo's convenient ones — so the two are separate rather than one shared helper
with flags, which is how a demo default reaches a classroom.

**This module never constructs a transport.** Import contract 2 forbids anything
under `services` from reaching the model SDK, including transitively, and that is
not a technicality to work around — it is what guarantees no call escapes the
`LLMCall` ledger. So the concrete transport is *injected* by a composition root
outside `services` (`scripts/serve.py`), and the default here is no model at all:
templates, shadow mode, and a record that says plainly it ran degraded. A server
started without wiring one is safe and honest rather than subtly unledgered.

Two defaults are deliberately awkward to change:

**Shadow mode is on unless explicitly disabled.** Phase 0's whole argument is
that generated text does not reach a child until the exit gates are met, and
`eval.harness.cli phase0` currently reports six outstanding. So this reads the
environment and defaults to shadow, meaning the switch that lets a model's words
reach a student is a deployment decision someone makes on purpose.

**Distress screening is on and cannot be silently unrouted.** `PipelineDeps`
refuses to compose screening without a destination, so a deployment that has not
configured an on-call path fails at startup instead of serving children with a
screen that alerts nobody.

**The rollout starts at nothing.** P1.1's staged rollout is read from the
database, and a deployment where nobody has recorded a setting has generation
off. So leaving shadow mode is not by itself enough to put a model's words in
front of a child: someone has to say so, in a row that carries their name and
their reason. Two switches rather than one is the point — the phase and the
percentage are different decisions, made at different times, on different
evidence.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable

from sqlalchemy.orm import Session as DbSession

from packages.llm import DatabaseLedger, LLMClient
from packages.llm.protocol import Transport
from packages.prompts import PromptRegistry
from packages.telemetry import DatabaseEventSink, EventRecorder
from services.orchestrator.hints import DatabaseHintSink
from services.orchestrator.review import DatabaseReviewSink
from services.orchestrator.rollout import DatabaseRolloutSource
from services.orchestrator.shadow import DatabaseShadowSink
from services.orchestrator.state import PipelineDeps
from services.orchestrator.symbolic_client import InProcessSymbolicChecker
from services.safety.alerts import ConsoleResponder, DatabaseAlertSink

TransportFactory = Callable[[], Transport]

_transport_factory: TransportFactory | None = None


def set_transport_factory(factory: TransportFactory | None) -> None:
    """Wire a model provider in, from outside `services`.

    Called by `scripts/serve.py`. Left unset, every stage takes its deterministic
    path — which is a real, tested mode, not a broken one.
    """
    global _transport_factory
    _transport_factory = factory


def _truthy(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def shadow_mode_enabled() -> bool:
    """Phase 0 default. `TUTOR_SHADOW_MODE=0` is the deliberate opt-out."""
    return _truthy("TUTOR_SHADOW_MODE", default=True)


def build_deps(db: DbSession, session_id: uuid.UUID) -> PipelineDeps:
    """Wire the pipeline for one request."""
    llm: LLMClient | None = None
    if _transport_factory is not None:
        llm = LLMClient(_transport_factory(), DatabaseLedger(db))

    shadow = shadow_mode_enabled()
    return PipelineDeps(
        recorder=EventRecorder(DatabaseEventSink(db), session_id),
        prompts=PromptRegistry(),
        llm=llm,
        symbolic=InProcessSymbolicChecker(),
        db=db,
        shadow_mode=shadow,
        shadow_sink=DatabaseShadowSink(db) if shadow else None,
        # P1.1's staged rollout and kill switch, read live per attempt. Always
        # wired, including in shadow mode: a served deployment that leaves
        # `shadow_mode` off must not be one where generation is ungated, and the
        # unconfigured state of this source is generation *off*. Turning it on is
        # then a recorded, attributed act rather than the absence of one.
        rollout=DatabaseRolloutSource(db),
        # §5's HintLog. Without it the record says a hint was shown but not what
        # it said, and a teacher reviewing the session cannot see what the child
        # read.
        hint_sink=DatabaseHintSink(db),
        # Without this the student page tells a child their teacher will look at
        # their work and nobody is told (§3.6). Phase 0 is 100% review, so in
        # shadow mode every finished session lands in the queue.
        review_sink=DatabaseReviewSink(db),
        screen_for_distress=True,
        # A development stand-in, and named as one. Replacing it is the P1.8
        # deliverable no code can supply: a defined on-call response.
        safety_sink=DatabaseAlertSink(db, ConsoleResponder()),
    )
