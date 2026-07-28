"""OpenTelemetry spans keyed by session (M0.8).

§8 wants cost and latency per pipeline stage, and §4 wants every transition
observable. The event log answers "what happened" durably; spans answer "how long
and how nested" while it is happening, and carry `session_id` so a trace joins to
the replay for the same session.

Tracing is off unless configured. An unconfigured OTel API is a no-op, so a test
or a script that never calls `configure_tracing` pays nothing and needs no
collector — the observability layer must not be a reason a stage cannot run.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from types import TracebackType
from uuid import UUID

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.trace import Status, StatusCode

from packages.domain.enums import PipelineStage

SESSION_ID_ATTRIBUTE = "tutor.session_id"
"""The join key. Every span carries it so a trace and a replay describe the same
session — §8's dashboards segment on it, and an incident starts from it."""

STAGE_ATTRIBUTE = "tutor.stage"
MODEL_ATTRIBUTE = "tutor.model_id"
PROMPT_VERSION_ATTRIBUTE = "tutor.prompt_version"
COST_ATTRIBUTE = "tutor.cost_usd"

_TRACER_NAME = "socratic-tutor"


def configure_tracing(exporter: SpanExporter | None = None, service_name: str = "tutor") -> None:
    """Install a tracer provider. Call once at startup; no-op tracing otherwise."""
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if exporter is not None:
        provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)


def tracer() -> trace.Tracer:
    return trace.get_tracer(_TRACER_NAME)


@contextmanager
def stage_span(
    stage: PipelineStage,
    session_id: UUID,
    **attributes: str | int | float | bool,
) -> Iterator[trace.Span]:
    """Wrap one pipeline stage in a span.

    Records an exception and marks the span as failed before re-raising, so a
    stage that blew up is visible as an error in the trace rather than as a span
    that merely ended early.
    """
    with tracer().start_as_current_span(f"stage.{stage.value}") as span:
        span.set_attribute(SESSION_ID_ATTRIBUTE, str(session_id))
        span.set_attribute(STAGE_ATTRIBUTE, stage.value)
        for key, value in attributes.items():
            span.set_attribute(f"tutor.{key}", value)
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            raise


def annotate_model_call(
    span: trace.Span, *, model_id: str, prompt_version: str, cost_usd: float
) -> None:
    """Attach the M0.4 ledger's key fields to the span covering the call."""
    span.set_attribute(MODEL_ATTRIBUTE, model_id)
    span.set_attribute(PROMPT_VERSION_ATTRIBUTE, prompt_version)
    span.set_attribute(COST_ATTRIBUTE, cost_usd)


class _NullSpan:
    def __enter__(self) -> None:
        return None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None
