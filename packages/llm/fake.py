"""Test transport (M0.4).

Every stage must be testable with zero network (Architecture.md §4). This is
what makes that true — it records what it was asked and returns what it was
scripted to return, including failures.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from packages.llm.models import TokenUsage
from packages.llm.protocol import TransportResponse


@dataclass
class RecordedCall:
    model_id: str
    system: str
    user_content: str
    max_tokens: int
    timeout_s: float
    max_retries: int


@dataclass
class FakeTransport:
    """Returns `reply` unless `raises` is set, in which case it raises it.

    `responder` exists because a single canned reply cannot model a multi-stage
    pipeline: the diagnoser expects JSON, the generator expects prose, and the
    leak-check classifier expects the single word SAFE or LEAK. A fake that
    answers all three identically makes stages fail for fixture reasons rather
    than real ones — which shows up as a confusing test failure instead of a
    finding. Given `(system, user_content)`, a responder can answer each stage in
    its own contract.
    """

    reply: str = "ok"
    responder: Callable[[str, str], str] | None = None
    usage: TokenUsage = field(
        default_factory=lambda: TokenUsage(input_tokens=100, output_tokens=20)
    )
    stop_reason: str | None = "end_turn"
    raises: Exception | None = None
    calls: list[RecordedCall] = field(default_factory=list)

    def complete(
        self,
        *,
        model_id: str,
        system: str,
        user_content: str,
        max_tokens: int,
        timeout_s: float,
        max_retries: int,
    ) -> TransportResponse:
        self.calls.append(
            RecordedCall(
                model_id=model_id,
                system=system,
                user_content=user_content,
                max_tokens=max_tokens,
                timeout_s=timeout_s,
                max_retries=max_retries,
            )
        )
        if self.raises is not None:
            raise self.raises
        text = self.responder(system, user_content) if self.responder else self.reply
        return TransportResponse(
            text=text,
            stop_reason=self.stop_reason,
            usage=self.usage,
            model_id=model_id,
            request_id="req_fake",
        )
