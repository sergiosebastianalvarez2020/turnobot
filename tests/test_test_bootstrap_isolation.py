"""Regressions for database isolation while importing the application in tests."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_IMPORT_PROBE = """
import os
from pathlib import Path
from unittest.mock import patch
import database.database as database
import database.pg_pool as pg_pool

initialized_backends = []
patch.object(
    database,
    "init_database",
    side_effect=lambda backend: initialized_backends.append(backend),
).start()
patch.object(pg_pool, "init_pg_pool", return_value=object()).start()
patch.object(pg_pool, "register_pg_pool_teardown", lambda app: None).start()

import tests.conftest
import app

assert initialized_backends == ["sqlite"], initialized_backends
assert app.app.config["DB_BACKEND"] == "sqlite"
assert app.app.config["DATABASE_URL"] is None
assert database.DATABASE_PATH != Path("database/appointments.db").resolve()
assert os.environ["DATABASE_URL"] == ""
"""


@pytest.mark.parametrize(
    "postgres_test_url", [True, False], ids=["with-pg-test-url", "without-pg-test-url"]
)
@pytest.mark.no_pg
def test_app_import_uses_isolated_sqlite_with_postgres_environment(postgres_test_url):
    env = os.environ.copy()
    env["DATABASE_URL"] = "postgresql://inherited:ignored@127.0.0.1:1/inherited"
    if postgres_test_url:
        env["TURNOBOT_PG_URL"] = "postgresql://test:isolated@127.0.0.1:1/test_maintenance"
    else:
        env.pop("TURNOBOT_PG_URL", None)
    env.pop("DB_BACKEND", None)
    env.pop("FLASK_ENV", None)
    env["PYTHONPATH"] = str(PROJECT_ROOT)

    result = subprocess.run(
        [sys.executable, "-c", _IMPORT_PROBE],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
