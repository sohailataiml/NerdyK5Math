"""The concrete Anthropic transport — the only module that imports the SDK.

Nothing in the pipeline imports this. Stages depend on `packages.llm.protocol`
(the interface) and `packages.llm.errors` (the failures), and a composition root
— an app entrypoint or a script — picks the implementation:

    from packages.llm.transport import AnthropicTransport
    client = LLMClient(AnthropicTransport(), DatabaseLedger(db))

Keeping the SDK behind that one import is what the "model SDK is reachable only
through packages.llm" contract is protecting, and it is why a stage can be
imported and tested in an environment where `anthropic` is not installed.
"""

from __future__ import annotations

from packages.llm.errors import RefusalError, TransportError
from packages.llm.models import TokenUsage
from packages.llm.protocol import TransportResponse


class AnthropicTransport:
    """Real provider calls.

    Retries and timeouts are delegated to the SDK, which already backs off on
    429 and 5xx. Hand-rolling that on top would multiply the wall-clock ceiling
    (`timeout x (retries + 1)`) past the §8 budget without adding resilience.
    """

    def __init__(self, api_key: str | None = None) -> None:
        # Imported here rather than at module scope so the package stays
        # importable — and unit tests stay runnable — without the SDK's
        # credential resolution running at import time.
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

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
        client = self._client.with_options(timeout=timeout_s, max_retries=max_retries)
        try:
            message = client.messages.create(
                model=model_id,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user_content}],
            )
        except self._anthropic.APIStatusError as exc:
            raise TransportError(f"{model_id} returned {exc.status_code}: {exc.message}") from exc
        except self._anthropic.APIConnectionError as exc:
            raise TransportError(f"could not reach the provider for {model_id}") from exc

        usage = TokenUsage(
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
            cache_read_tokens=message.usage.cache_read_input_tokens or 0,
            cache_write_tokens=message.usage.cache_creation_input_tokens or 0,
        )

        if message.stop_reason == "refusal":
            raise RefusalError(
                f"{model_id} declined the request",
                usage=usage,
                model_id=message.model,
                request_id=message._request_id,
            )

        text = next((b.text for b in message.content if b.type == "text"), "")
        return TransportResponse(
            text=text,
            stop_reason=message.stop_reason,
            usage=usage,
            model_id=message.model,
            request_id=message._request_id,
        )
