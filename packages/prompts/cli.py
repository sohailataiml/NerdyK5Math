"""Prompt library management (M0.6).

`verify` runs in CI. It is what turns "published prompts are immutable" from a
convention into a build failure, and it is why the `prompt_version` on an
`LLMCall` can be trusted months later.

Usage::

    python -m packages.prompts.cli verify
    python -m packages.prompts.cli list
    python -m packages.prompts.cli publish diagnose/K-1/v1
"""

from __future__ import annotations

import argparse
import sys

from packages.prompts.registry import PromptError, PromptRegistry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="packages.prompts.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("verify", help="check published prompts still match their locked hashes")
    sub.add_parser("list", help="show every prompt in the library")
    publish = sub.add_parser("publish", help="record a version's hash as published")
    publish.add_argument("name", help="e.g. diagnose/K-1/v1")

    args = parser.parse_args(argv)
    registry = PromptRegistry()

    if args.command == "list":
        lock = registry.lock()
        print(f"\n{len(registry.templates)} prompt(s):\n")
        for name, template in sorted(registry.templates.items()):
            state = "published" if name in lock else "UNPUBLISHED"
            slots = ", ".join(sorted(template.slots())) or "-"
            print(f"  {name:<28} {template.content_hash}  {state}")
            print(f"    slots: {slots}")
            if template.untrusted:
                print(f"    untrusted: {', '.join(sorted(template.untrusted))}")
        print()
        return 0

    if args.command == "publish":
        try:
            digest = registry.publish(args.name)
        except PromptError as exc:
            print(f"\n  {exc}\n")
            return 1
        print(f"\n  published {args.name} @ {digest}\n")
        return 0

    problems = registry.verify()
    if problems:
        print(f"\n=== prompts: FAIL ({len(problems)} problem(s)) ===")
        for problem in problems:
            print(f"  {problem}")
        print()
        return 1
    print(f"\n=== prompts: OK ({len(registry.templates)} published, all hashes match) ===\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
