"""The transport interface, with no implementation attached.

Split from `transport.py` so that everything upstream of the provider — the
client, the stages, the tests — depends on the *shape* of a model call without
importing the SDK. Only the composition root (an app entrypoint or a script)
names a concrete transport.

That is what makes "every stage is independently testable with zero model calls"
(§4) true rather than aspirational: a stage can be imported and exercised in an
environment where `anthropic` is not installed at all.
"""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict

from packages.llm.models import TokenUsage


class TransportResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    stop_reason: str | None
    usage: TokenUsage
    model_id: str
    request_id: str | None = None


class Transport(Protocol):
    def complete(
        self,
        *,
        model_id: str,
        system: str,
        user_content: str,
        max_tokens: int,
        timeout_s: float,
        max_retries: int,
    ) -> TransportResponse: ...
