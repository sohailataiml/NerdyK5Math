"""Pipeline stages.

Stages never import each other — composition happens in ``graph.py`` so that
retries, timeouts, and logging live in one place (§6). Enforced by the
"Pipeline stages are independent" import contract."""
