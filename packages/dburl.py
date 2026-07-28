"""Normalising a hosted provider's `DATABASE_URL` to the driver this repo uses.

Managed Postgres providers hand out `postgres://` or `postgresql://`. Neither
works here, and both fail in a way that reads as something else:

- `postgres://` — SQLAlchemy 2 removed support for the scheme outright and
  raises `NoSuchModuleError`.
- `postgresql://` — SQLAlchemy resolves it to **psycopg2**, which this project
  does not install (it uses psycopg 3). The error names a missing module rather
  than a wrong URL, so the first guess is a broken build, not a scheme.

Both surface at startup on the first deploy, which is the worst moment to be
debugging a URL. Normalising once, here, means the same string works locally,
in CI, and on a host.

Everything else is passed through untouched — `sqlite://` for the fast test
suite, and an explicitly-driven `postgresql+psycopg://` for anyone who already
got it right.
"""

from __future__ import annotations

DRIVER = "postgresql+psycopg"

_BARE_SCHEMES = ("postgres://", "postgresql://")


def normalize(url: str) -> str:
    """Return `url` with a bare Postgres scheme pinned to psycopg 3.

    >>> normalize("postgres://u:p@host/db")
    'postgresql+psycopg://u:p@host/db'
    >>> normalize("sqlite://")
    'sqlite://'
    """
    for scheme in _BARE_SCHEMES:
        if url.startswith(scheme):
            return f"{DRIVER}://{url[len(scheme) :]}"
    return url
