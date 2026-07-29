"""Event log, replay, and tracing (M0.8).

The durable half (`events`, `replay`) is what makes §4's "reconstruct any
session" true. The live half (`tracing`) is what makes §8's per-stage cost and
latency dashboards possible. Both key on `session_id` so they describe the same
thing.
"""

from packages.telemetry.economics import Economics, Segment, economics
from packages.telemetry.events import (
    DatabaseEventSink,
    EventRecorder,
    EventSink,
    InMemoryEventSink,
)
from packages.telemetry.replay import ReplayStep, SessionReplay, replay
from packages.telemetry.trace import SessionTrace, StageRun, trace, trace_from
from packages.telemetry.tracing import (
    SESSION_ID_ATTRIBUTE,
    annotate_model_call,
    configure_tracing,
    stage_span,
    tracer,
)

__all__ = [
    "SESSION_ID_ATTRIBUTE",
    "DatabaseEventSink",
    "Economics",
    "EventRecorder",
    "EventSink",
    "InMemoryEventSink",
    "ReplayStep",
    "Segment",
    "SessionReplay",
    "SessionTrace",
    "StageRun",
    "annotate_model_call",
    "configure_tracing",
    "economics",
    "replay",
    "stage_span",
    "trace",
    "trace_from",
    "tracer",
]
