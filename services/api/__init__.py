"""FastAPI surface (§6).

Currently the teacher console only (P0.8) — Phase 0's data-collection
instrument. The student-facing endpoints (`/sessions`, the SSE hint stream) are
not built; Phase 0 needs teachers rating shadow output before a student client
is worth building against a pipeline nobody has measured yet.
"""

from services.api.app import app

__all__ = ["app"]
