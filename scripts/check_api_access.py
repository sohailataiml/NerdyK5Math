"""Diagnose provider access without spending credits.

Separates the three failure modes that all surface as "the call didn't work":
a bad key (401), an org with no credit balance (400 on inference but the Models
API still answers), and a reachable, funded account.

Run::

    .venv/Scripts/python -m scripts.check_api_access
"""

from __future__ import annotations

import os
import sys

import anthropic
from dotenv import load_dotenv


def main() -> int:
    load_dotenv(".env", override=True)
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("ANTHROPIC_API_KEY is not set.")
        return 1
    print(f"key: {key[:18]}...{key[-6:]}  (len {len(key)})")

    client = anthropic.Anthropic(api_key=key)

    # Models API costs nothing — it isolates auth from billing.
    try:
        models = list(client.models.list())
        print(f"auth:      OK — Models API returned {len(models)} models")
        print(f"           e.g. {', '.join(m.id for m in models[:4])}")
    except anthropic.AuthenticationError:
        print("auth:      FAILED — key is invalid or revoked (401)")
        return 1
    except anthropic.APIStatusError as exc:
        print(f"auth:      Models API returned {exc.status_code}: {exc.message}")
        return 1

    # Smallest possible inference call — 1 output token.
    try:
        client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1,
            messages=[{"role": "user", "content": "hi"}],
        )
        print("inference: OK — the account can make model calls")
        return 0
    except anthropic.APIStatusError as exc:
        credit = "credit balance" in (exc.message or "").lower()
        label = "NO CREDIT on this organization" if credit else f"{exc.status_code}"
        print(f"inference: FAILED — {label}")
        if credit:
            print("           The key authenticates; the org needs credits at")
            print("           console.anthropic.com -> Plans & Billing.")
        else:
            print(f"           {exc.message}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
