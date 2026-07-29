"""The single entry point for every model call in the system (M0.4).

Two guarantees, both structural rather than conventional:

1. **No call escapes the ledger.** The provider call sits inside a `try/finally`
   whose `finally` writes the `LLMCall` row — success, refusal, timeout, or
   crash. A stage cannot opt out, because it has no way to reach the transport
   except through here (enforced by the SDK import contract in pyproject.toml).
2. **No student PII reaches a prompt.** `complete()` accepts a `PromptContext`,
   whose fields cannot express a student name or profile. This is
   Implementation-Plan.md M0.10's requirement made a type error rather than a
   review checklist item — student data leaving the system boundary on every
   request is the compliance cost this architecture revision takes on, and the
   narrow point where it is containable is prompt construction.
"""

from __future__ import annotations

import datetime as dt
import time
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from packages.domain.enums import GradeBand, PipelineStage
from packages.domain.models import LLMCall
from packages.llm.config import StageConfig, config_for
from packages.llm.errors import RefusalError, TransportError
from packages.llm.ledger import LedgerWriter
from packages.llm.models import TIER_MODELS, TokenUsage, cost_usd
from packages.llm.protocol import Transport, TransportResponse
from packages.prompts.registry import RenderedPrompt


class PromptContext(BaseModel):
    """Everything a stage is allowed to put in front of a model.

    Note what is *absent*: no student name, no student ID, no profile, no IEP
    flags. Those exist in `packages.domain` but are unreachable from here, so a
    prompt cannot carry them even by accident. `session_id` is an opaque UUID —
    enough to correlate a call with its session for audit, useless as an
    identifier to the provider.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: UUID
    grade_band: GradeBand
    problem_prompt: str
    correct_answer: str | None = None
    student_answer: str | None = None
    attempt_number: int = Field(default=1, ge=1)
    extra: dict[str, str] = Field(default_factory=dict)

    def as_payload(self) -> dict[str, object]:
        """The input side of the ledger row."""
        return self.model_dump(mode="json")


class LLMResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    llm_call_id: UUID
    model_id: str
    usage: TokenUsage
    latency_ms: int
    cost_usd: float


class LLMClient:
    """Tier-aware, ledgered, timeout-bounded model access."""

    def __init__(self, transport: Transport, ledger: LedgerWriter) -> None:
        self._transport = transport
        self._ledger = ledger

    def complete(
        self,
        *,
        stage: PipelineStage,
        context: PromptContext,
        prompt: RenderedPrompt,
    ) -> LLMResult:
        """Make one model call.

        Takes a `RenderedPrompt` rather than loose strings plus a version label,
        so the version recorded in the ledger cannot disagree with the text that
        was actually sent (M0.6). With separate arguments, a caller editing the
        prompt and forgetting the version produces an audit trail that is wrong
        in the one way nobody checks.
        """
        config = config_for(stage)
        model_id = TIER_MODELS[config.tier]
        started = time.perf_counter()
        call_id: UUID | None = None

        response: TransportResponse | None = None
        error: TransportError | None = None
        try:
            response = self._transport.complete(
                model_id=model_id,
                system=prompt.system,
                user_content=prompt.user,
                max_tokens=config.max_tokens,
                timeout_s=config.timeout_s,
                max_retries=config.max_retries,
            )
        except TransportError as exc:
            error = exc
            raise
        finally:
            # Runs on the success path, on TransportError, and on anything else
            # propagating out of the transport. This is the whole point.
            call_id = self._record(
                stage=stage,
                context=context,
                prompt=prompt,
                config=config,
                model_id=model_id,
                latency_ms=_elapsed_ms(started),
                response=response,
                error=error,
            )

        assert response is not None  # the try/except above guarantees this
        return LLMResult(
            text=response.text,
            llm_call_id=call_id,
            model_id=response.model_id,
            usage=response.usage,
            latency_ms=_elapsed_ms(started),
            cost_usd=float(cost_usd(response.model_id, response.usage)),
        )

    def _record(
        self,
        *,
        stage: PipelineStage,
        context: PromptContext,
        prompt: RenderedPrompt,
        config: StageConfig,
        model_id: str,
        latency_ms: int,
        response: TransportResponse | None,
        error: TransportError | None,
    ) -> UUID:
        usage = _usage_of(response, error)
        billed_model = _billed_model(response, error, model_id)

        if response is not None:
            output: dict[str, object] = {
                "text": response.text,
                "stop_reason": response.stop_reason,
                "request_id": response.request_id,
            }
        else:
            output = {
                "error": type(error).__name__ if error else "UnknownError",
                "message": str(error) if error else "call failed before a response",
                "refused": isinstance(error, RefusalError),
                "request_id": error.request_id if error else None,
            }

        call = LLMCall(
            session_id=context.session_id,
            stage=stage,
            model_id=billed_model,
            prompt_version=prompt.version,
            input_payload={
                **context.as_payload(),
                # The hash pins the exact prompt text behind this call, so an
                # audit can prove which wording produced a grade even after the
                # library has moved on (§8 segments quality by prompt version).
                "prompt_content_hash": prompt.content_hash,
                # And the text as sent. The hash above pins the *template*; this
                # is the rendered result, which is per-call and cannot be
                # recovered from the template plus the context — `generate_hint`
                # substitutes a strategy and hint level, and `leak_check` a hint,
                # none of which `PromptContext` carries. Without this, replaying
                # a prompt for those two stages would mean re-rendering with
                # missing values and showing a plausible prompt that was never
                # sent. §12's argument is that a grade can be defended later, and
                # the wording that produced it is half of that defence.
                #
                # No new exposure: the context beside it already carries the
                # child's answer and `correct_answer`, and this whole row is
                # behind M0.9's `can_read_audit_trail`.
                "rendered_prompt": {"system": prompt.system, "user": prompt.user},
                "max_tokens": config.max_tokens,
                "timeout_s": config.timeout_s,
            },
            output_payload=output,
            tokens_in=usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens,
            tokens_out=usage.output_tokens,
            latency_ms=latency_ms,
            cost_usd=float(cost_usd(billed_model, usage)),
            created_at=dt.datetime.now(dt.UTC),
        )
        self._ledger.record(call)
        return call.id


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _usage_of(response: TransportResponse | None, error: TransportError | None) -> TokenUsage:
    if response is not None:
        return response.usage
    if error is not None and error.usage is not None:
        return error.usage
    # A call that failed before the provider reported usage genuinely cost
    # nothing — a timeout or connection error, not a billed request.
    return TokenUsage(input_tokens=0, output_tokens=0)


def _billed_model(
    response: TransportResponse | None, error: TransportError | None, requested: str
) -> str:
    """Prefer the model the provider says served the request over the one asked for."""
    if response is not None:
        return response.model_id
    if error is not None and error.model_id is not None:
        return error.model_id
    return requested
