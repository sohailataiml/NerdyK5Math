"""Normalising a hosted provider's DATABASE_URL.

Small, but it runs before anything else on a deploy — `alembic upgrade head` is
the start command's first clause — and both failure modes name the wrong thing.
`postgres://` raises `NoSuchModuleError`, and `postgresql://` resolves to
psycopg2, which is not installed, so the error is a missing module rather than a
wrong scheme. Either one reads as a broken build on the first deploy.
"""

from __future__ import annotations

import pytest

from packages.dburl import normalize


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        # What Render, Heroku, and Fly actually hand out.
        (
            "postgres://u:p@dpg-abc.oregon-postgres.render.com/tutor",
            "postgresql+psycopg://u:p@dpg-abc.oregon-postgres.render.com/tutor",
        ),
        (
            "postgresql://u:p@host:5432/tutor",
            "postgresql+psycopg://u:p@host:5432/tutor",
        ),
    ],
)
def test_bare_schemes_are_pinned_to_psycopg3(given: str, expected: str) -> None:
    assert normalize(given) == expected


def test_an_explicit_driver_is_left_alone() -> None:
    """Someone who already got it right must not be rewritten."""
    url = "postgresql+psycopg://tutor:tutor@localhost:5433/tutor"

    assert normalize(url) == url


def test_sqlite_is_untouched() -> None:
    """The whole unit suite runs on this; rewriting it would break every test
    rather than the one that is wrong."""
    assert normalize("sqlite://") == "sqlite://"
    assert normalize("sqlite:///tmp/x.db") == "sqlite:///tmp/x.db"


def test_credentials_containing_the_scheme_are_not_mangled() -> None:
    """A password can contain almost anything. Only the leading scheme is
    replaced, and only once — a naive `str.replace` would corrupt a password
    that happens to contain the substring."""
    url = "postgres://user:postgres://weird@host/db"

    assert normalize(url) == "postgresql+psycopg://user:postgres://weird@host/db"
