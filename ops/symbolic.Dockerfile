# Symbolic equivalence service (M0.7).
#
# This is the one component that evaluates attacker-influenced input, so it is
# isolated at the container level as well as in code: unprivileged user,
# read-only root filesystem, no network egress, capped CPU and memory (see
# ops/docker-compose.yml). A compromise here should reach nothing.

FROM python:3.12-slim

# Dependencies are installed as root, then everything runs as nobody.
RUN pip install --no-cache-dir \
    "sympy>=1.13" \
    "fastapi>=0.115" \
    "uvicorn[standard]>=0.32" \
    "pydantic>=2.9"

WORKDIR /app
COPY services/symbolic /app/services/symbolic
COPY services/__init__.py /app/services/__init__.py

# No shell, no package manager use at runtime, no writable application code.
USER nobody

EXPOSE 8000

# One worker: the workload is CPU-bound and short, and concurrency here would
# only make the memory cap harder to reason about.
CMD ["uvicorn", "services.symbolic.app:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
