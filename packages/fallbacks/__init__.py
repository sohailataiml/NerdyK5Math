"""Deterministic degradation paths (M0.12).

Rule pre-check, keyed lookup, and the template hint library. These are what keep
the pipeline serving students when the model provider is down (Architecture.md
§4), and what Phase 0 serves students with while the models run in shadow — so
they are production paths, not stubs.

**Known limit, stated rather than discovered later.** The degradation path covers
*numeric* answers only. `answer_leak` cannot verify an algebraic or free-text
answer deterministically, so it fails closed on one — meaning that with the
provider down and a non-numeric answer, no hint can be cleared for display and
the attempt must escalate to a teacher. That is the correct behaviour (a leak
cannot be taken back) but it is a real coverage gap: the deterministic fallback
serves the K-5 arithmetic strand, not the whole K-12 range. Extending it means
extending `answer_leak`, not loosening it.
"""

from packages.fallbacks.answer_leak import (
    CHECKER_VERSION,
    LeakVerdict,
    check_hint,
    check_template_safety,
    numbers_in,
    parse_number,
)
from packages.fallbacks.lookup import GENERAL_STRATEGY, LookupResult, lookup
from packages.fallbacks.rules import RULES, RuleDiagnosis, diagnose, parse_problem
from packages.fallbacks.templates import (
    LIBRARY,
    HintTemplate,
    RenderedTemplate,
    TemplateError,
    render,
)

__all__ = [
    "CHECKER_VERSION",
    "GENERAL_STRATEGY",
    "LIBRARY",
    "RULES",
    "HintTemplate",
    "LeakVerdict",
    "LookupResult",
    "RenderedTemplate",
    "RuleDiagnosis",
    "TemplateError",
    "check_hint",
    "check_template_safety",
    "diagnose",
    "lookup",
    "numbers_in",
    "parse_number",
    "parse_problem",
    "render",
]
