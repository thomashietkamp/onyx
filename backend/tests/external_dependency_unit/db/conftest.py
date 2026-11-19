"""Fixtures for testing DAL classes against a real PostgreSQL database.

These fixtures build on the db_session and tenant_context fixtures from
the parent conftest (tests/external_dependency_unit/conftest.py).

Requires a running Postgres instance. Run with::

    python -m dotenv -f .vscode/.env run -- pytest tests/external_dependency_unit/db/
"""

from collections.abc import Generator

import pytest

from tests.external_dependency_unit.db.shard_test_utils import temporary_database


@pytest.fixture(scope="module")
def second_database() -> Generator[str, None, None]:
    """A real second database for the shard suites.

    Module-scoped so each suite gets its own, rather than sharing state through a
    database that another module is reconfiguring shards against.
    """
    yield from temporary_database("onyx_shard_test")
