"""Tests unitarios de la CLI ``scripts/migrate_pg.py`` (sin PostgreSQL real).

Cubren D7 (flag ``--lock-timeout``) y D8 (carga de ``.env``): el parser acepta y
propaga el timeout, rechaza valores inválidos, no filtra credenciales en el
error de lock y la CLI invoca ``load_dotenv()`` al importar respetando la
precedencia del entorno existente.

Todo corre sin servidor: ``get_database_backend`` solo mira el esquema de la URL
y ``run_migrations`` se reemplaza por un doble.
"""

import importlib
import os

import dotenv
import pytest

import scripts.migrate_pg as migrate_pg
from database.pg_migrator import MigrationLockTimeout, MigrationResult

PG_URL = "postgresql://turnobot:secret@localhost:5432/turnobot"


@pytest.fixture(autouse=True)
def pg_test_env():
    """Tests puros con mocks: sin PostgreSQL real, sin skip funcional del conftest."""
    yield


def _fake_result(**overrides) -> MigrationResult:
    base = dict(dry_run=True, adopted=[], applied=[], pending=[], current_version=0)
    base.update(overrides)
    return MigrationResult(**base)


# ---------------------------------------------------------------------------
# D7 — flag --lock-timeout
# ---------------------------------------------------------------------------


def test_lock_timeout_omitido_usa_default_del_migrador(monkeypatch):
    captured = {}

    def fake_run(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return _fake_result()

    monkeypatch.setattr(migrate_pg, "run_migrations", fake_run)

    code = migrate_pg.main(["--url", PG_URL, "--dry-run"])

    assert code == 0
    assert captured["url"] == PG_URL
    assert captured["lock_timeout"] is None


def test_lock_timeout_se_propaga_al_api(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        migrate_pg,
        "run_migrations",
        lambda url, **kwargs: captured.update(kwargs) or _fake_result(),
    )

    code = migrate_pg.main(["--url", PG_URL, "--lock-timeout", "12.5"])

    assert code == 0
    assert captured["lock_timeout"] == 12.5


@pytest.mark.parametrize("value", ["0", "-1", "-0.5", "abc", ""])
def test_lock_timeout_invalido_rechazado(value):
    """Valores no positivos o no numéricos son error de argparse (SystemExit 2)."""
    with pytest.raises(SystemExit) as exc_info:
        migrate_pg.main(["--url", PG_URL, "--lock-timeout", value])

    assert exc_info.value.code == 2


def test_lock_timeout_error_no_expone_credenciales(monkeypatch, capsys):
    def fake_run(url, **kwargs):
        raise MigrationLockTimeout(f"no se pudo tomar el lock de {PG_URL}")

    monkeypatch.setattr(migrate_pg, "run_migrations", fake_run)

    code = migrate_pg.main(["--url", PG_URL, "--lock-timeout", "0.5"])

    assert code == 1
    err = capsys.readouterr().err
    assert "ERROR" in err
    assert "secret" not in err
    assert "***" in err


def test_parser_lock_timeout_default_none():
    parsed = migrate_pg.build_parser().parse_args(["--url", PG_URL])
    assert parsed.lock_timeout is None


# ---------------------------------------------------------------------------
# D8 — carga de .env
# ---------------------------------------------------------------------------


def test_cli_invoca_load_dotenv_al_importar(monkeypatch):
    """``migrate_pg`` debe llamar a ``load_dotenv()`` durante el import.

    Al ejecutarse en el import (antes de construir el parser y de leer la URL),
    el entorno del proceso queda disponible para el valor por defecto de ``--url``.
    """
    calls = []

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return False

    monkeypatch.setattr(dotenv, "load_dotenv", spy)
    importlib.reload(migrate_pg)

    assert calls, "migrate_pg debe invocar load_dotenv() al importar"
    assert all(kwargs.get("override") is not True for kwargs in calls)


def test_cli_carga_valores_de_un_dotenv(monkeypatch, tmp_path):
    """Los valores del ``.env`` deben quedar en el entorno del proceso."""
    env_file = tmp_path / ".env"
    env_file.write_text("TURNOBOT_D8_SENTINEL=from_dotenv\n", encoding="utf-8")
    real_load = dotenv.load_dotenv

    def fake_load(*args, **kwargs):
        return real_load(dotenv_path=str(env_file), override=kwargs.get("override", False))

    monkeypatch.delenv("TURNOBOT_D8_SENTINEL", raising=False)
    monkeypatch.setattr(dotenv, "load_dotenv", fake_load)
    try:
        importlib.reload(migrate_pg)
        assert os.environ.get("TURNOBOT_D8_SENTINEL") == "from_dotenv"
    finally:
        os.environ.pop("TURNOBOT_D8_SENTINEL", None)


def test_cli_dotenv_no_pisa_entorno_existente(monkeypatch, tmp_path):
    """Precedencia: una variable ya definida en el entorno no se sobrescribe."""
    env_file = tmp_path / ".env"
    env_file.write_text("DATABASE_URL=postgresql://from_file@host/db\n", encoding="utf-8")
    real_load = dotenv.load_dotenv

    def fake_load(*args, **kwargs):
        return real_load(dotenv_path=str(env_file), override=kwargs.get("override", False))

    monkeypatch.setenv("DATABASE_URL", PG_URL)
    monkeypatch.setattr(dotenv, "load_dotenv", fake_load)

    importlib.reload(migrate_pg)

    assert os.environ["DATABASE_URL"] == PG_URL


def test_cli_usa_database_url_del_entorno_por_defecto(monkeypatch):
    captured = {}
    monkeypatch.setenv("DATABASE_URL", PG_URL)
    monkeypatch.setattr(
        migrate_pg,
        "run_migrations",
        lambda url, **kwargs: captured.update(url=url) or _fake_result(),
    )

    code = migrate_pg.main(["--dry-run"])

    assert code == 0
    assert captured["url"] == PG_URL


def test_cli_sin_url_ni_entorno_falla_controlado(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DB_BACKEND", raising=False)

    code = migrate_pg.main(["--dry-run"])

    assert code == 1
    assert "DATABASE_URL" in capsys.readouterr().err
