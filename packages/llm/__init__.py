"""Claude client wrapper (M0.4).

Owns per-stage tiering, timeouts, bounded retry, and the mandatory ``LLMCall``
ledger write.

**`AnthropicTransport` is deliberately not exported here.** Importing it would
put the SDK in the dependency graph of everything that touches this package,
including the pipeline stages — which is the thing the "model SDK is reachable
only through packages.llm" contract exists to prevent. The concrete transport is
chosen at a composition root::

    from packages.llm.transport import AnthropicTransport

Everything upstream depends on ``protocol.Transport`` and ``errors``, so a stage
can be imported and tested where ``anthropic`` is not installed at all.
"""

from packages.llm.client import LLMClient, LLMResult, PromptContext
from packages.llm.config import STAGE_CONFIG, StageConfig, config_for
from packages.llm.errors import RefusalError, TransportError
from packages.llm.ledger import DatabaseLedger, InMemoryLedger, LedgerWriter
from packages.llm.models import (
    PRICING,
    TIER_MODELS,
    ModelTier,
    TokenUsage,
    UnknownModelError,
    cost_usd,
    pricing_key,
)
from packages.llm.protocol import Transport, TransportResponse

__all__ = [
    "PRICING",
    "STAGE_CONFIG",
    "TIER_MODELS",
    "DatabaseLedger",
    "InMemoryLedger",
    "LLMClient",
    "LLMResult",
    "LedgerWriter",
    "ModelTier",
    "PromptContext",
    "RefusalError",
    "StageConfig",
    "TokenUsage",
    "Transport",
    "TransportError",
    "TransportResponse",
    "UnknownModelError",
    "config_for",
    "cost_usd",
    "pricing_key",
]
