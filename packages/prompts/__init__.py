"""Versioned prompt modules (M0.6).

Published versions are immutable; a change is a new version, gated on an eval
run. Nothing outside this package constructs a prompt string — the client takes
a ``RenderedPrompt``, which only ``PromptRegistry.render`` produces.
"""

from packages.prompts.registry import (
    LIBRARY_DIR,
    LOCK_PATH,
    SHARED_BAND,
    STUDENT_FACING_STAGES,
    PromptError,
    PromptRegistry,
    PromptTemplate,
    RenderedPrompt,
    parse,
)

__all__ = [
    "LIBRARY_DIR",
    "LOCK_PATH",
    "SHARED_BAND",
    "STUDENT_FACING_STAGES",
    "PromptError",
    "PromptRegistry",
    "PromptTemplate",
    "RenderedPrompt",
    "parse",
]
