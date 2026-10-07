"""Regresiones de hardening para restore/verify PostgreSQL (Fase 5).

Cubre vulnerabilidades reales más allá de la barrera productiva base
(tests/test_restore_postgresql_safety.py):

- bases reservadas del sistema (postgres/template0/template1) rechazadas;
- inyección de opciones CLI vía DBNAME rechazada;
- identificadores inválidos rechazados;
- separador "--" y timeouts en dropdb/createdb/pg_restore;
- pg_restore con error -> el script falla (no ignora el código de salida);
- verify read-only: solo ejecuta `pg_restore --list` con timeout;
- dump inválido -> rechazado;
- secretos nunca en comandos ni en mensajes de error.

Tests puros con mocks: ninguna operación destructiva contra PostgreSQL real.
"""

from types import SimpleNamespace

import pytest

from scripts import restore_postgresql, verify_backup_postgresql

PROD_URL = "postgresql://turnobot:secret@prod.example.com:5432/turnobot"


@pytest.fixture(autouse=True)
def pg_test_env():
    """Tests puros: sin PostgreSQL real, sin skip funcional del conftest."""
    yield


def _fake_result(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _run_restore(monkeypatch, tmp_path, dbname, rc=0, stderr=""):
    backup = tmp_path / "turnobot-pg.dump"
    backup.write_bytes(b"FAKE")
    calls = []
    kwargs_seen = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        kwargs_seen.append(kwargs)
        return _fake_result(returncode=rc, stderr=stderr)

    monkeypatch.setattr(restore_postgresql.shutil, "which", lambda name: "/bin/" + name)
    monkeypatch.setattr(restore_postgresql.subprocess, "run", fake_run)
    monkeypatch.setenv("DATABASE_URL", PROD_URL)
    return calls, kwargs_seen


# --------------------------------------------------------------------------- #
# Bases reservadas del sistema
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("reserved", ["postgres", "template0", "template1",
                                      "POSTGRES", "Template1"])
def test_validate_rechaza_bases_reservadas(reserved):
    with pytest.raises(ValueError, match="reservada"):
        restore_postgresql.validate_restore_target(
            reserved, "prod.example.com", "5432", PROD_URL
        )


@pytest.mark.parametrize("reserved", ["postgres", "template0", "template1"])
def test_restore_rechaza_reservada_sin_subprocess(monkeypatch, tmp_path, reserved):
    backup = tmp_path / "turnobot-pg.dump"
    backup.write_bytes(b"FAKE")
    calls = []
    monkeypatch.setattr(restore_postgresql.shutil, "which", lambda name: "/bin/" + name)
    monkeypatch.setattr(
        restore_postgresql.subprocess,
        "run",
        lambda cmd, **kw: (calls.append(list(cmd)), _fake_result())[1],
    )
    monkeypatch.setenv("DATABASE_URL", PROD_URL)
    with pytest.raises(ValueError, match="reservada"):
        restore_postgresql.restore_database(backup, target_dbname=reserved)
    assert calls == []


# --------------------------------------------------------------------------- #
# Inyección de opciones CLI / identificadores inválidos
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "evil",
    [
        "--host=evil.example.com",
        "-e",
        "a;b",
        "a b",
        "x$HOME",
        "x`id`",
        "turnobot;DROP TABLE",
        "",
        "a" * 64,  # límite PostgreSQL: 63 bytes
        "0empieza_con_digito",
    ],
)
def test_validate_rechaza_nombres_peligrosos(evil):
    with pytest.raises(ValueError, match="reservada|válido"):
        restore_postgresql.validate_restore_target(
            evil, "prod.example.com", "5432", PROD_URL
        )


def test_restore_rechaza_inyeccion_sin_subprocess(monkeypatch, tmp_path):
    backup = tmp_path / "turnobot-pg.dump"
    backup.write_bytes(b"FAKE")
    calls = []
    monkeypatch.setattr(restore_postgresql.shutil, "which", lambda name: "/bin/" + name)
    monkeypatch.setattr(
        restore_postgresql.subprocess,
        "run",
        lambda cmd, **kw: (calls.append(list(cmd)), _fake_result())[1],
    )
    monkeypatch.setenv("DATABASE_URL", PROD_URL)
    with pytest.raises(ValueError, match="válido"):
        restore_postgresql.restore_database(
            backup, target_dbname="--host=evil.example.com"
        )
    assert calls == []


def test_validate_permite_identificador_valido():
    restore_postgresql.validate_restore_target(
        "turnobot_restore_20260101_000000", "prod.example.com", "5432", PROD_URL
    )


# --------------------------------------------------------------------------- #
# Separador "--" y timeouts en comandos destructivos
# --------------------------------------------------------------------------- #


def test_restore_usa_separador_y_timeout(monkeypatch, tmp_path):
    calls, kwargs_seen = _run_restore(
        monkeypatch, tmp_path, "turnobot_restore_probe"
    )
    backup = tmp_path / "turnobot-pg.dump"
    restore_postgresql.restore_database(backup, target_dbname="turnobot_restore_probe")
    dropdb = [c for c in calls if c[:1] == ["dropdb"]]
    createdb = [c for c in calls if c[:1] == ["createdb"]]
    assert dropdb and "--" in dropdb[0]
    assert createdb and "--" in createdb[0]
    for kw in kwargs_seen:
        assert "timeout" in kw and kw["timeout"] is not None


# --------------------------------------------------------------------------- #
# pg_restore con error -> el script falla; sin fuga de secretos
# --------------------------------------------------------------------------- #


def test_restore_falla_si_pg_restore_falla(monkeypatch, tmp_path):
    backup = tmp_path / "turnobot-pg.dump"
    backup.write_bytes(b"FAKE")
    seen = {}

    def fake_run(cmd, **kwargs):
        if cmd[0] == "pg_restore":
            env = kwargs.get("env", {})
            seen["cmd"] = list(cmd)
            seen["env"] = dict(env)
            return _fake_result(returncode=1, stderr="pg_restore: error fatal")
        return _fake_result()

    monkeypatch.setattr(restore_postgresql.shutil, "which", lambda name: "/bin/" + name)
    monkeypatch.setattr(restore_postgresql.subprocess, "run", fake_run)
    monkeypatch.setenv("DATABASE_URL", PROD_URL)
    with pytest.raises(RuntimeError, match="pg_restore falló"):
        restore_postgresql.restore_database(
            backup, target_dbname="turnobot_restore_probe"
        )
    # La contraseña viaja solo en el entorno, nunca en argv ni en el error.
    assert not any("secret" in part for part in seen["cmd"])
    assert seen["env"].get("PGPASSWORD") == "secret"


def test_error_pg_restore_no_filtra_password(monkeypatch, tmp_path):
    backup = tmp_path / "turnobot-pg.dump"
    backup.write_bytes(b"FAKE")

    def fake_run(cmd, **kwargs):
        if cmd[0] == "pg_restore":
            return _fake_result(returncode=1, stderr="connection failed for db")
        return _fake_result()

    monkeypatch.setattr(restore_postgresql.shutil, "which", lambda name: "/bin/" + name)
    monkeypatch.setattr(restore_postgresql.subprocess, "run", fake_run)
    monkeypatch.setenv("DATABASE_URL", PROD_URL)
    with pytest.raises(RuntimeError) as excinfo:
        restore_postgresql.restore_database(
            backup, target_dbname="turnobot_restore_probe"
        )
    assert "secret" not in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Verify: read-only, con timeout, rechaza dumps inválidos
# --------------------------------------------------------------------------- #


def test_verify_es_read_only_con_timeout(monkeypatch, tmp_path):
    backup = tmp_path / "turnobot-pg.dump"
    backup.write_bytes(b"\x00" * 2048)
    calls = []
    seen = {}

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        seen.update(kwargs)
        return _fake_result()

    monkeypatch.setattr(
        verify_backup_postgresql.shutil, "which", lambda name: "/bin/" + name
    )
    monkeypatch.setattr(verify_backup_postgresql.subprocess, "run", fake_run)
    assert verify_backup_postgresql.verify_backup(backup) is True
    # Solo pg_restore --list: no dropdb/createdb/psql/pg_restore(restore).
    assert len(calls) == 1
    assert calls[0][:2] == ["pg_restore", "--list"]
    assert "timeout" in seen and seen["timeout"] is not None
    # Sin credenciales en el entorno del verify.
    assert "PGPASSWORD" not in (seen.get("env") or {})


def test_verify_rechaza_dump_invalido(monkeypatch, tmp_path):
    backup = tmp_path / "turnobot-pg.dump"
    backup.write_bytes(b"TRUNCADO")
    monkeypatch.setattr(
        verify_backup_postgresql.shutil, "which", lambda name: "/bin/" + name
    )
    monkeypatch.setattr(
        verify_backup_postgresql.subprocess,
        "run",
        lambda cmd, **kw: _fake_result(returncode=1, stderr="not a valid archive"),
    )
    with pytest.raises(RuntimeError, match="Backup inválido"):
        verify_backup_postgresql.verify_backup(backup)


def test_verify_rechaza_backup_vacio(monkeypatch, tmp_path):
    backup = tmp_path / "turnobot-pg.dump"
    backup.write_bytes(b"")
    monkeypatch.setattr(
        verify_backup_postgresql.shutil, "which", lambda name: "/bin/" + name
    )
    with pytest.raises(RuntimeError, match="vacío o corrupto"):
        verify_backup_postgresql.verify_backup(backup)
