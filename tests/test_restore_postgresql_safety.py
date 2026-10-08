"""Regresiones de seguridad para scripts/restore_postgresql.py.

Validan que la barrera rechaza destinos productivos ANTES de ejecutar dropdb,
permite destinos temporales, falla seguro ante configuracion ambigua y que la
CLI explica el uso seguro.

Son tests puros: no requieren PostgreSQL real. Se anula el skip funcional del
conftest (tests/conftest.py omite toda la suite cuando TURNOBOT_PG_URL no esta
definida) porque aqui no se abre ninguna conexion.
"""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import restore_postgresql

ROOT = Path(restore_postgresql.__file__).resolve().parents[1]
PROD_URL = "postgresql://turnobot:secret@prod.example.com:5432/turnobot"


@pytest.fixture(autouse=True)
def pg_test_env():
    """Tests puros: no se necesita PostgreSQL, por lo que no se aplica el skip
    funcional del conftest (omite la suite cuando TURNOBOT_PG_URL no esta
    definida)."""
    yield


def _fake_result(returncode: int = 0, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


# --------------------------------------------------------------------------- #
# Caso A — destino seguro (temporal)
# --------------------------------------------------------------------------- #


def test_parse_database_identity_extrae_componentes():
    ident = restore_postgresql.parse_database_identity(PROD_URL)
    assert ident == {
        "host": "prod.example.com",
        "port": "5432",
        "database": "turnobot",
        "user": "turnobot",
    }


def test_parse_database_identity_normaliza_postgres_prefix_y_host_minuscula():
    ident = restore_postgresql.parse_database_identity(
        "postgres://User@HOST.EXAMPLE.com:5433/mi_db"
    )
    assert ident["host"] == "host.example.com"
    assert ident["port"] == "5433"
    assert ident["database"] == "mi_db"


def test_is_dangerous_destination_permite_nombre_temporal():
    assert (
        restore_postgresql.is_dangerous_destination(
            "turnobot_restore_20260101_000000", "prod.example.com", "5432", PROD_URL
        )
        is False
    )


def test_validate_restore_target_no_lanza_para_destino_temporal():
    restore_postgresql.validate_restore_target(
        "turnobot_restore_probe", "prod.example.com", "5432", PROD_URL
    )


# --------------------------------------------------------------------------- #
# Caso B — destino peligroso (coincide con produccion)
# --------------------------------------------------------------------------- #


def test_is_dangerous_destination_rechaza_coincidencia_completa():
    assert (
        restore_postgresql.is_dangerous_destination(
            "turnobot", "prod.example.com", "5432", PROD_URL
        )
        is True
    )


def test_validate_restore_target_lanza_si_coincide_con_produccion():
    with pytest.raises(ValueError, match="coincide con la base productiva"):
        restore_postgresql.validate_restore_target("turnobot", "prod.example.com", "5432", PROD_URL)


def test_restore_database_rechaza_productivo_sin_dropdb(monkeypatch, tmp_path):
    """Caso B: un destino productivo se rechaza y NUNCA se ejecuta dropdb."""
    backup = tmp_path / "turnobot-pg.dump"
    backup.write_bytes(b"FAKE")
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _fake_result()

    monkeypatch.setattr(restore_postgresql.shutil, "which", lambda name: "/bin/" + name)
    monkeypatch.setattr(restore_postgresql.subprocess, "run", fake_run)
    monkeypatch.setenv("DATABASE_URL", PROD_URL)

    with pytest.raises(ValueError, match="coincide con la base productiva"):
        restore_postgresql.restore_database(backup, target_dbname="turnobot")

    # La barrera se dispara antes de dropdb: ningun subprocess debe invocarse.
    assert calls == []
    assert ["dropdb", "--if-exists", "turnobot"] not in calls


def test_restore_database_permite_temporal_y_ejecuta_dropdb_sobre_temporal(monkeypatch, tmp_path):
    """Caso A: un destino temporal valido avanza y ejecuta dropdb sobre el
    temporal, jamas sobre la base productiva."""
    backup = tmp_path / "turnobot-pg.dump"
    backup.write_bytes(b"FAKE")
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _fake_result()

    monkeypatch.setattr(restore_postgresql.shutil, "which", lambda name: "/bin/" + name)
    monkeypatch.setattr(restore_postgresql.subprocess, "run", fake_run)
    monkeypatch.setenv("DATABASE_URL", PROD_URL)

    target = "turnobot_restore_probe"
    returned = restore_postgresql.restore_database(backup, target_dbname=target)

    assert returned == target
    dropdb_calls = [c for c in calls if c[:1] == ["dropdb"]]
    assert len(dropdb_calls) == 1
    assert dropdb_calls[0] == ["dropdb", "--if-exists", "--", target]
    assert ["dropdb", "--if-exists", "turnobot"] not in calls


# --------------------------------------------------------------------------- #
# Caso C — mismo nombre en otro servidor no productivo: permitido
# --------------------------------------------------------------------------- #


def test_is_dangerous_destination_permite_mismo_nombre_en_host_distinto():
    assert (
        restore_postgresql.is_dangerous_destination(
            "turnobot", "staging.example.com", "5432", PROD_URL
        )
        is False
    )


def test_is_dangerous_destination_permite_mismo_nombre_en_puerto_distinto():
    assert (
        restore_postgresql.is_dangerous_destination(
            "turnobot", "prod.example.com", "5433", PROD_URL
        )
        is False
    )


# --------------------------------------------------------------------------- #
# Caso D — configuracion ambigua/missing: fail-safe
# --------------------------------------------------------------------------- #


def test_validate_restore_target_falla_seguro_si_url_invalida():
    with pytest.raises(ValueError, match="no se pudo identificar"):
        restore_postgresql.validate_restore_target(
            "turnobot", "prod.example.com", "5432", "not-a-valid-url"
        )


def test_is_dangerous_destination_falla_seguro_si_no_se_identifica_produccion():
    assert (
        restore_postgresql.is_dangerous_destination(
            "turnobot", "prod.example.com", "5432", "not-a-valid-url"
        )
        is True
    )


def test_restore_database_sin_database_url_no_ejecuta_dropdb(monkeypatch, tmp_path):
    """Sin DATABASE_URL, get_database_url falla antes de dropdb (fail-safe)."""
    backup = tmp_path / "turnobot-pg.dump"
    backup.write_bytes(b"FAKE")
    calls: list[list[str]] = []

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TURNOBOT_PG_URL", raising=False)
    monkeypatch.setattr(restore_postgresql.shutil, "which", lambda name: "/bin/" + name)
    monkeypatch.setattr(
        restore_postgresql.subprocess,
        "run",
        lambda cmd, **kw: (calls.append(list(cmd)), _fake_result())[1],
    )

    with pytest.raises(ValueError, match="debe estar configurada"):
        restore_postgresql.restore_database(backup, target_dbname="turnobot")

    assert calls == []


# --------------------------------------------------------------------------- #
# Caso E — CLI: --help explica destino seguro
# --------------------------------------------------------------------------- #


def test_cli_help_explica_destino_seguro():
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.restore_postgresql", "--help"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert proc.returncode == 0
    assert "productiva" in proc.stdout.lower()
    assert "temporal" in proc.stdout.lower()


def test_cli_sin_args_muestra_uso_y_sale_error():
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.restore_postgresql"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert proc.returncode == 1
    lower = proc.stdout.lower()
    assert "temporal" in lower or "productiva" in lower
