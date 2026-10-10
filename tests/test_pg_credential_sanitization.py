"""Regresiones D3: credenciales PostgreSQL nunca expuestas en logs, errores ni reportes.

Corrección D3 (docs/PG_MIGRATOR_AUDIT_BASELINE.md, sección D3).

`sanitize_database_url()` solo enmascaraba la forma URL completa
(``postgresql://user:pass@host/...``) y los mensajes de error de la CLI del
migrador podían llegar a emitir la URL/DSN original sin enmascarar.

Cubre:

- URLs con contraseñas de caracteres especiales codificados (``%XX``);
- DSN clave/valor aceptados por libpq/psycopg (``host=.. user=.. password=..``);
- variantes ``pwd=``, mayúsculas, espacios múltiples y valores citados;
- enmascarado de subcadenas embebidas en mensajes de error, preservando host,
  base y tipo de error;
- salida del CLI (``Destino:`` y ramas de error y logger) sin la contraseña real.

Tests puros con mocks: no requieren TURNOBOT_PG_URL ni tocan PostgreSQL real.
"""

import logging
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest import mock

import pytest

from database.pg_migrator import MigrationError, MigrationResult
from database.pg_pool import sanitize_database_url
from scripts import migrate_pg as migrate_pg_cli

URL_SECRET = "SeCrEt-Url.Pw"
DSN_SECRET = "SeCrEt-Dsn.Pw"
SECRET_URL = f"postgresql://deploy:{URL_SECRET}@db.example.com:5432/turnobot"
SECRET_DSN = f"host=db.example.com user=deploy password={DSN_SECRET} dbname=turnobot"


@pytest.fixture(autouse=True)
def pg_test_env():
    """Tests puros con mocks: sin PostgreSQL real, sin skip funcional del conftest."""
    yield


def _run_cli(argv):
    stdout, stderr = StringIO(), StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = migrate_pg_cli.main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


def test_url_password_masked():
    masked = sanitize_database_url(SECRET_URL)
    assert masked == "postgresql://deploy:***@db.example.com:5432/turnobot"
    assert URL_SECRET not in masked


def test_url_password_with_special_encoded_chars_masked():
    url = "postgresql://deploy:P%40ss%3Aw%2F0rd%5E%21@db.example.com:5432/turnobot"
    masked = sanitize_database_url(url)
    assert masked == "postgresql://deploy:***@db.example.com:5432/turnobot"
    assert "P%40ss%3Aw%2F0rd%5E%21" not in masked


def test_url_variants_and_noop_cases():
    cases = (
        (None, None),
        ("sqlite:///app.db", "sqlite:///app.db"),
        (
            "postgresql://deploy@db.example.com/turnobot",
            "postgresql://deploy@db.example.com/turnobot",
        ),
        ("postgres://u:secret@host:5432/db", "postgres://u:***@host:5432/db"),
    )
    for raw, expected in cases:
        assert sanitize_database_url(raw) == expected


def test_key_value_dsn_password_masked():
    masked = sanitize_database_url(SECRET_DSN)
    assert masked == "host=db.example.com user=deploy password=*** dbname=turnobot"
    assert DSN_SECRET not in masked
    assert "db.example.com" in masked and "turnobot" in masked and "deploy" in masked


@pytest.mark.parametrize(
    "dsn",
    (
        "host=db user=u pwd=secret dbname=x",
        "HOST=db USER=u PASSWORD=secret DBNAME=x",
        "host = db   user = u   PassWord = secret   dbname = x",
        "host=db user=u password='secret with spaces' dbname=x",
        "password=secret host=db dbname=x user=u",
    ),
)
def test_key_value_dsn_variants_masked(dsn):
    masked = sanitize_database_url(dsn)
    assert "secret" not in masked
    assert "***" in masked
    assert "db" in masked
    assert "x" in masked
    assert "u" in masked


def test_url_and_dsn_masked_inside_error_message():
    text_url = f"psycopg: FATAL 08P01 no se pudo conectar ({SECRET_URL})"
    masked_url = sanitize_database_url(text_url)
    assert URL_SECRET not in masked_url
    assert "db.example.com" in masked_url
    assert "FATAL" in masked_url

    text_dsn = f"psycopg: connection failed ({SECRET_DSN})"
    masked_dsn = sanitize_database_url(text_dsn)
    assert DSN_SECRET not in masked_dsn
    assert "db.example.com" in masked_dsn


def test_cli_destino_print_masks_url_password():
    result = MigrationResult(dry_run=True, adopted=[], applied=[], pending=[], current_version=1)
    with mock.patch.object(migrate_pg_cli, "run_migrations", return_value=result):
        code, stdout, stderr = _run_cli(["--url", SECRET_URL, "--dry-run"])
    assert code == 0
    assert "Destino: postgresql://deploy:***@db.example.com:5432/turnobot" in stdout
    combined = stdout + stderr
    assert URL_SECRET not in combined


def test_cli_generic_error_log_masks_password(caplog):
    error = RuntimeError(f"Fallo aplicando migraciones: {SECRET_URL}")
    with mock.patch.object(migrate_pg_cli, "run_migrations", side_effect=error):
        with caplog.at_level(logging.ERROR, logger="turnobot.pg_migrator.cli"):
            code, stdout, stderr = _run_cli(["--url", SECRET_URL])
    assert code == 1
    assert "Fallo aplicando migraciones" in caplog.text
    combined = stdout + stderr + caplog.text
    assert URL_SECRET not in combined
    assert "://deploy:***@" in combined


def test_cli_migration_error_uses_safe_repr():
    leaky = MigrationError(f"schema rollback ({SECRET_URL})")
    with mock.patch.object(migrate_pg_cli, "run_migrations", side_effect=leaky):
        code, stdout, stderr = _run_cli(["--url", SECRET_URL])
    assert code == 1
    combined = stdout + stderr
    assert URL_SECRET not in combined
    assert "://deploy:***@" in combined


def test_cli_backend_error_never_exposes_dsn_password():
    code, stdout, stderr = _run_cli(["--url", SECRET_DSN])
    assert code == 1
    combined = stdout + stderr
    assert DSN_SECRET not in combined
    assert "ERROR:" in combined
