"""Run the server (P0.8, P0.11).

A composition root, and it lives in `scripts/` for a structural reason rather
than a stylistic one. Import contract 2 forbids anything under `services` from
reaching the model SDK, even transitively, because that rule is what guarantees
no model call escapes the `LLMCall` ledger. Something still has to choose a
concrete provider, so that choice is made here and injected.

Run::

    docker compose -f ops/docker-compose.yml up -d
    .venv/Scripts/alembic upgrade head
    .venv/Scripts/python -m scripts.serve

Then the child's page is at http://localhost:8080/?as=<student-principal-id> and
the teacher's at /teacher/rate?as=<teacher-principal-id>. `scripts.seed_pilot`
prints both.

Without `ANTHROPIC_API_KEY` the server runs fully deterministic: rule diagnosis,
keyed lookup, template hints, pattern-only welfare screening. That is a real
mode, not a broken one — it is what a provider outage looks like from a child's
side, and the record says so on every session.
"""

from __future__ import annotations

import argparse
import os
import sys

import uvicorn
from dotenv import load_dotenv

from services.api.pipeline import set_transport_factory, shadow_mode_enabled


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scripts.serve")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args(argv)

    load_dotenv(".env", override=True)

    if os.environ.get("ANTHROPIC_API_KEY"):
        from packages.llm.transport import AnthropicTransport

        set_transport_factory(AnthropicTransport)
        provider = "live model calls"
    else:
        provider = "NO PROVIDER — deterministic paths only"

    shadow = shadow_mode_enabled()
    print(f"\n  provider:    {provider}")
    print(f"  shadow mode: {'ON — generated hints are recorded, never shown' if shadow else 'OFF'}")
    if not shadow:
        # Worth shouting about. `eval.harness.cli phase0` is the check that says
        # whether this is allowed yet, and it currently says no.
        print("  !! generated text will reach students. Confirm the Phase 0 gates first:")
        print("     python -m eval.harness.cli phase0")
    print(f"\n  student:  http://{args.host}:{args.port}/?as=<student-principal-id>")
    print(f"  teacher:  http://{args.host}:{args.port}/teacher/rate?as=<teacher-principal-id>")
    print("  ids:      python -m scripts.seed_pilot\n")

    uvicorn.run("services.api.app:app", host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
