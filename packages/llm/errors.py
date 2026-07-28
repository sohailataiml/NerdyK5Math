"""Model-call failures, separated from the transport that raises them.

These live apart from `transport.py` for a structural reason the import contract
surfaced: a stage needs to *handle* a provider failure, but handling one should
not put the SDK in that stage's dependency graph. With the errors here, a stage
depends on the interface rather than the implementation — which is exactly what
"the model SDK is reachable only through packages.llm" is meant to guarantee.

The practical payoff: `services/orchestrator/stages/*` can catch a transport
failure and degrade, while remaining importable and testable with no SDK present
at all.
"""

from __future__ import annotations

from packages.llm.models import TokenUsage


class TransportError(RuntimeError):
    """A call reached the provider boundary and failed.

    Carries whatever usage the provider reported before failing, so the ledger
    records real spend rather than zeros. A failed call that cost tokens and
    logs none is precisely the gap that makes a cost dashboard lie.
    """

    def __init__(
        self,
        message: str,
        *,
        usage: TokenUsage | None = None,
        model_id: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.usage = usage
        self.model_id = model_id
        self.request_id = request_id


class RefusalError(TransportError):
    """The provider's safety classifiers declined the request.

    This arrives as a successful HTTP 200 with `stop_reason: "refusal"` and an
    empty or partial content list — not as an exception — so code that reads
    `content[0]` without checking breaks on it.

    It is a foreseeable outcome here, not an exotic one: §7 routes free-text
    student responses through a distress screen, and self-harm language in a
    child's answer is exactly the shape of input a classifier may decline.
    Surfacing it as its own error type lets the safety stage route to the
    counselor alert path instead of treating it as a transport failure.
    """
