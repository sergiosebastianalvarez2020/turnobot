"""Base class for PostgreSQL-based tests using pytest fixtures with unittest compatibility.

Usage:
    class MyTest(PostgreSQLTestCase):
        def test_something(self, app, client):
            # app and client fixtures are provided
            # use self.app, self.client for convenience
            result = self.client.get("/")
            assert result.status_code == 200

    Or use pytest fixtures directly in method signatures:
    class MyTest(PostgreSQLTestCase):
        def test_something(self, app_ctx):
            # app_ctx provides a Flask app context for direct DB access
            from database.database import get_connection
            conn = get_connection()
            conn.close()

The autouse fixture _inject_pg_fixtures_instance injects app, client, pg_test_url
as instance attributes (self.app, self.client, self.pg_test_url).
"""

import pytest

import database.database as database


class PostgreSQLTestCase:
    """Base class for tests that need a PostgreSQL database.

    Subclasses get:
    - self.app: Flask app configured for a temp PostgreSQL database
    - self.client: Flask test client
    - self.pg_test_url: PostgreSQL conninfo string for the temp database

    The autouse fixture creates the app and client for each test, with a
    fresh PostgreSQL database. The app context is NOT pushed automatically;
    tests must request app_ctx or use self.app.app_context() / self.client.
    """

    @pytest.fixture(autouse=True)
    def _inject_pg_fixtures_instance(self, app, client, pg_test_url):
        self.app = app
        self.client = client
        self.pg_test_url = pg_test_url


def get_test_connection():
    """Get a database connection for direct SQL execution in tests.

    Requires an active Flask app context (use app_ctx fixture or self.app.app_context()).
    """
    return database.get_connection()


def execute_sql(sql: str, params: tuple = ()):
    """Execute SQL directly against the test database."""
    conn = get_test_connection()
    try:
        cursor = conn.execute(sql, params)
        conn.commit()
        return cursor
    finally:
        conn.close()


def query_sql(sql: str, params: tuple = ()):
    """Query SQL directly against the test database."""
    conn = get_test_connection()
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()
