"""Pruebas unitarias de la guardia de seguridad de los helpers PostgreSQL.

Estas pruebas NO requieren un servidor PostgreSQL: ``_validate_test_db_name``
valida únicamente el nombre de la base de datos, y las funciones
``create_test_database``/``drop_test_database`` validan antes de conectar.
"""

import pytest

from tests_pg._helpers import (
    _TEST_DB_NAME_RE,
    _validate_test_db_name,
    create_test_database,
    drop_test_database,
    new_db_name,
)


# Mantiene el patrón exacto del helper `new_db_name`.
def _valid_test_name(suffix: str) -> str:
    return f"turnobot_test_{suffix}"


# Mantiene el patrón exacto del helper `new_db_name`.
def _valid_bootstrap_name(suffix: str) -> str:
    return f"turnobot_bootstrap_{suffix}"


@pytest.mark.parametrize(
    "dbname",
    [
        _valid_test_name("1" * 32),
        _valid_bootstrap_name("1" * 10),
        _valid_test_name("0" * 32),
        _valid_bootstrap_name("abcdef1234567890"),
        # Sólo minúsculas (uuid4().hex es siempre minúsculas)
        _valid_test_name("a" * 32),
        _valid_bootstrap_name("abcdef123456789a"),
    ],
)
def test_valid_test_db_names_pass(dbname: str) -> None:
    assert _TEST_DB_NAME_RE.fullmatch(dbname) is not None
    _validate_test_db_name(dbname)  # no raise


@pytest.mark.parametrize(
    "dbname",
    [
        "postgres",
        "template1",
        "turnobot_prod_1",
        "turnobot_staging_1",
        "my_database",
        "turnobot_test",
        "turnobot_bootstrap",
        "turnobot_test_abc!",
        "turnobot_test_",
        "turnobot_test_123_ABC",  # mayúsculas no admitidas por el patrón seguro
        "",
        "other_test_123",
    ],
)
def test_invalid_test_db_names_rejected(dbname: str) -> None:
    with pytest.raises(ValueError):
        _validate_test_db_name(dbname)


def test_create_database_rejects_non_test_names() -> None:
    with pytest.raises(ValueError):
        create_test_database("postgresql://localhost:5433/postgres", "postgres")


def test_drop_database_rejects_non_test_names() -> None:
    with pytest.raises(ValueError):
        drop_test_database("postgresql://localhost:5433/postgres", "postgres")


def test_new_db_name_always_returns_valid_name() -> None:
    for _ in range(50):
        name = new_db_name()
        assert _TEST_DB_NAME_RE.fullmatch(name) is not None
