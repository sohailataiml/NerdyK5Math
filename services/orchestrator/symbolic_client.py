"""HTTP client for the symbolic service (§3.5, M0.7).

The orchestrator reaches the checker over the network rather than importing it,
because that service is isolated on purpose: it evaluates untrusted student input
inside a container with no egress, a read-only filesystem, and a memory cap. An
in-process call would move that evaluation into the orchestrator and discard
every one of those protections.
"""

from __future__ import annotations

import httpx


class HttpSymbolicChecker:
    """Calls `POST /equivalent` on the symbolic service."""

    def __init__(self, base_url: str = "http://symbolic:8000", timeout_s: float = 5.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_s

    def equivalent(self, *, expected: str, actual: str) -> bool:
        response = httpx.post(
            f"{self._base_url}/equivalent",
            json={"expected": expected, "actual": actual},
            timeout=self._timeout,
        )
        if response.status_code == 422:
            # A malformed *expected* answer is a curriculum bug. Grading the
            # child wrong for it would blame them for our content error, so it
            # raises and the graph escalates.
            raise ValueError(f"symbolic service rejected the expected answer: {response.text}")
        response.raise_for_status()
        equivalent: bool = response.json()["equivalent"]
        return equivalent


class InProcessSymbolicChecker:
    """Direct call, for tests and for the single-process demo script.

    Not for production: it gives up the container isolation that is the whole
    reason the symbolic service exists as a separate process.
    """

    def equivalent(self, *, expected: str, actual: str) -> bool:
        from services.symbolic.equivalence import check

        return check(expected, actual).equivalent
