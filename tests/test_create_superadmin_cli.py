"""Tests for the standalone SUPERADMIN bootstrap CLI."""

from types import SimpleNamespace
from unittest.mock import Mock

import psycopg
import pytest

from database import pg_pool
from scripts import create_superadmin


@pytest.mark.no_pg
def test_create_superadmin_uses_initialized_app_pool_and_closes_it(monkeypatch):
    pool = object()
    flask_app = SimpleNamespace(config={"DB_BACKEND": "postgresql"}, extensions={"pg_pool": pool})
    application_module = SimpleNamespace(app=flask_app)
    backend_resolved = []
    monkeypatch.setattr(
        pg_pool,
        "get_database_backend",
        Mock(side_effect=lambda: backend_resolved.append("postgresql") or "postgresql"),
    )

    def import_application(module_name):
        assert module_name == "app"
        assert backend_resolved == ["postgresql"]
        return application_module

    monkeypatch.setattr(create_superadmin.importlib, "import_module", import_application)
    close_pool = Mock()
    monkeypatch.setattr(pg_pool, "close_pg_pool", close_pool)
    monkeypatch.setattr(pg_pool, "get_pg_pool", Mock(return_value=None))
    create_user = Mock(return_value={"success": True, "platform_user_id": 42})
    monkeypatch.setattr(create_superadmin.platform, "create_superadmin", create_user)

    result = create_superadmin._create_superadmin("operator@example.test", "long-enough-password")

    assert result == {"success": True, "platform_user_id": 42}
    create_user.assert_called_once_with(
        "operator@example.test", "long-enough-password", "operator@example.test"
    )
    close_pool.assert_called_once_with(app=flask_app)


@pytest.mark.no_pg
def test_sqlite_backend_is_rejected_before_app_import_or_migrations(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("FLASK_ENV", "development")
    monkeypatch.setenv("DB_BACKEND", "sqlite")
    load_environment = Mock()
    monkeypatch.setattr(create_superadmin, "load_dotenv", load_environment)
    prompt = Mock()
    monkeypatch.setattr(create_superadmin, "getpass", prompt)
    import_application = Mock(side_effect=AssertionError("app must not be imported"))
    monkeypatch.setattr(create_superadmin.importlib, "import_module", import_application)

    from database import database

    run_sqlite_migrations = Mock()
    monkeypatch.setattr(database, "apply_migrations", run_sqlite_migrations)

    with pytest.raises(RuntimeError, match="debe ser PostgreSQL"):
        create_superadmin.main(["--email", "operator@example.test"])

    prompt.assert_not_called()
    load_environment.assert_called_once_with(dotenv_path=create_superadmin.PROJECT_ROOT / ".env")
    import_application.assert_not_called()
    run_sqlite_migrations.assert_not_called()


@pytest.mark.no_pg
def test_postgresql_environment_backend_validation_does_not_connect(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://db.example.test:5432/turnobot_test")
    monkeypatch.setenv("FLASK_ENV", "production")
    load_environment = Mock()
    monkeypatch.setattr(create_superadmin, "load_dotenv", load_environment)
    connect = Mock(side_effect=AssertionError("backend validation must not connect"))
    create_pool = Mock(side_effect=AssertionError("backend validation must not create a pool"))
    initialize_pool = Mock(
        side_effect=AssertionError("backend validation must not initialize a pool")
    )
    monkeypatch.setattr(psycopg, "connect", connect)
    monkeypatch.setattr(pg_pool, "create_pg_pool", create_pool)
    monkeypatch.setattr(pg_pool, "init_pg_pool", initialize_pool)

    create_superadmin._require_postgresql_backend()

    load_environment.assert_called_once_with(dotenv_path=create_superadmin.PROJECT_ROOT / ".env")
    connect.assert_not_called()
    create_pool.assert_not_called()
    initialize_pool.assert_not_called()


@pytest.mark.no_pg
def test_create_app_failure_closes_pool_created_during_partial_initialization(monkeypatch):
    partial_pool = object()
    monkeypatch.setattr(pg_pool, "get_database_backend", Mock(return_value="postgresql"))
    monkeypatch.setattr(pg_pool, "get_pg_pool", Mock(side_effect=[None, partial_pool]))
    import_application = Mock(side_effect=RuntimeError("database initialization failed"))
    monkeypatch.setattr(create_superadmin.importlib, "import_module", import_application)
    close_pool = Mock()
    monkeypatch.setattr(pg_pool, "close_pg_pool", close_pool)

    with pytest.raises(RuntimeError, match="database initialization failed"):
        create_superadmin._create_superadmin("operator@example.test", "long-enough-password")

    close_pool.assert_called_once_with(pool=partial_pool)


@pytest.mark.no_pg
def test_create_app_failure_without_pool_does_not_close_uninitialized_resources(monkeypatch):
    monkeypatch.setattr(pg_pool, "get_database_backend", Mock(return_value="postgresql"))
    monkeypatch.setattr(pg_pool, "get_pg_pool", Mock(return_value=None))
    monkeypatch.setattr(
        create_superadmin.importlib,
        "import_module",
        Mock(side_effect=RuntimeError("application import failed")),
    )
    close_pool = Mock()
    monkeypatch.setattr(pg_pool, "close_pg_pool", close_pool)

    with pytest.raises(RuntimeError, match="application import failed"):
        create_superadmin._create_superadmin("operator@example.test", "long-enough-password")

    close_pool.assert_not_called()


@pytest.mark.no_pg
def test_create_superadmin_closes_pool_if_service_raises(monkeypatch):
    flask_app = SimpleNamespace(
        config={"DB_BACKEND": "postgresql"}, extensions={"pg_pool": object()}
    )
    monkeypatch.setattr(pg_pool, "get_database_backend", Mock(return_value="postgresql"))
    monkeypatch.setattr(
        create_superadmin.importlib,
        "import_module",
        Mock(return_value=SimpleNamespace(app=flask_app)),
    )
    monkeypatch.setattr(pg_pool, "get_pg_pool", Mock(return_value=None))
    close_pool = Mock()
    monkeypatch.setattr(pg_pool, "close_pg_pool", close_pool)
    monkeypatch.setattr(
        create_superadmin.platform,
        "create_superadmin",
        Mock(side_effect=RuntimeError("database failure")),
    )

    with pytest.raises(RuntimeError, match="database failure"):
        create_superadmin._create_superadmin("operator@example.test", "long-enough-password")

    close_pool.assert_called_once_with(app=flask_app)


@pytest.mark.no_pg
def test_cleanup_failure_does_not_mask_service_exception(monkeypatch):
    flask_app = SimpleNamespace(
        config={"DB_BACKEND": "postgresql"}, extensions={"pg_pool": object()}
    )
    monkeypatch.setattr(pg_pool, "get_database_backend", Mock(return_value="postgresql"))
    monkeypatch.setattr(
        create_superadmin.importlib,
        "import_module",
        Mock(return_value=SimpleNamespace(app=flask_app)),
    )
    monkeypatch.setattr(pg_pool, "get_pg_pool", Mock(return_value=None))
    monkeypatch.setattr(pg_pool, "close_pg_pool", Mock(side_effect=OSError("cleanup failure")))
    monkeypatch.setattr(
        create_superadmin.platform,
        "create_superadmin",
        Mock(side_effect=RuntimeError("original service failure")),
    )

    with pytest.raises(RuntimeError, match="original service failure") as error:
        create_superadmin._create_superadmin("operator@example.test", "long-enough-password")

    assert any("OSError" in note for note in error.value.__notes__)


@pytest.mark.no_pg
def test_main_prompts_for_password_and_never_accepts_password_argument(monkeypatch, capsys):
    password = "long-enough-password"
    monkeypatch.setattr(pg_pool, "get_database_backend", Mock(return_value="postgresql"))
    create_user = Mock(return_value={"success": True, "platform_user_id": 42})
    prompt = Mock(return_value=password)
    monkeypatch.setattr(create_superadmin, "getpass", prompt)
    monkeypatch.setattr(create_superadmin, "_create_superadmin", create_user)

    assert create_superadmin.main(["--email", "operator@example.test"]) == 0

    prompt.assert_called_once_with("Contraseña del superadmin: ")
    create_user.assert_called_once_with("operator@example.test", password)
    assert password not in capsys.readouterr().out


@pytest.mark.no_pg
def test_main_rejects_password_command_line_argument(monkeypatch):
    prompt = Mock()
    monkeypatch.setattr(create_superadmin, "getpass", prompt)

    with pytest.raises(SystemExit):
        create_superadmin.main(
            ["--email", "operator@example.test", "--password", "long-enough-password"]
        )

    prompt.assert_not_called()


@pytest.mark.no_pg
def test_main_rechaza_password_menor_a_12_caracteres(monkeypatch):
    monkeypatch.setattr(
        pg_pool, "resolve_database_backend", Mock(return_value=("postgresql", None))
    )
    monkeypatch.setattr(create_superadmin, "getpass", Mock(return_value="short"))
    create_user = Mock()
    monkeypatch.setattr(create_superadmin, "_create_superadmin", create_user)

    assert create_superadmin.main(["--email", "operator@example.test"]) == 1
    create_user.assert_not_called()
