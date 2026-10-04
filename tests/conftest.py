import pytest

import config


@pytest.fixture(autouse=True)
def no_real_database(monkeypatch):
    """Never let a test reach the DATABASE_URL loaded from a local .env.

    That URL is the production database. Tests that exercise storage set
    their own value and fake the connection.
    """
    monkeypatch.setattr(config, "DATABASE_URL", "")
