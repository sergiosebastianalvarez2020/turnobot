"""Tests para la preparación del backend PostgreSQL y la configuración (Fase 4C)."""

import os
import unittest
from unittest import mock

from flask import Flask

from application.config import build_config
from database.pg_pool import (
    close_pg_pool,
    create_pg_pool,
    get_database_backend,
    get_pg_pool,
    init_pg_pool,
    normalize_database_url,
    sanitize_database_url,
)


class TestPostgresConfig(unittest.TestCase):
    def setUp(self):
        # Asegurar aislamiento de variables de entorno
        self.env_patcher = mock.patch.dict(os.environ, {}, clear=False)
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()
        close_pg_pool()

    def test_database_url_normalization(self):
        """Verifica la normalización de URLs postgres:// a postgresql://."""
        self.assertIsNone(normalize_database_url(None))
        self.assertIsNone(normalize_database_url(""))
        self.assertEqual(
            normalize_database_url("postgres://user:pass@localhost:5432/db"),
            "postgresql://user:pass@localhost:5432/db",
        )
        self.assertEqual(
            normalize_database_url("postgresql://user:pass@localhost:5432/db"),
            "postgresql://user:pass@localhost:5432/db",
        )

    def test_database_url_sanitization(self):
        """Verifica que las contraseñas en URLs de DB se oculten en logs y configs."""
        self.assertIsNone(sanitize_database_url(None))
        self.assertEqual(
            sanitize_database_url("postgresql://user:secret123@localhost:5432/turnobot"),
            "postgresql://user:***@localhost:5432/turnobot",
        )
        self.assertEqual(sanitize_database_url("sqlite:///app.db"), "sqlite:///app.db")

    def test_database_backend_detection(self):
        """Verifica la detección correcta del backend segun DATABASE_URL."""
        self.assertEqual(get_database_backend(None), "sqlite")
        self.assertEqual(get_database_backend(""), "sqlite")
        self.assertEqual(get_database_backend("sqlite:///database.db"), "sqlite")
        self.assertEqual(get_database_backend("postgresql://user:pass@localhost/db"), "postgresql")
        self.assertEqual(get_database_backend("postgres://user:pass@localhost/db"), "postgresql")

    def test_build_config_default_sqlite(self):
        """Verifica que la app use SQLite por defecto si DATABASE_URL no está configurada."""
        os.environ.pop("DATABASE_URL", None)
        app = Flask(__name__)
        config = build_config(app)

        self.assertEqual(config["DB_BACKEND"], "sqlite")
        self.assertIsNone(config["DATABASE_URL_SANITIZED"])
        self.assertEqual(app.config["DB_BACKEND"], "sqlite")

    def test_build_config_postgresql_sanitized(self):
        """Verifica que build_config cargue DATABASE_URL y oculte credenciales."""
        os.environ["DATABASE_URL"] = "postgres://admin:pass123@dbhost:5432/turnodb"
        app = Flask(__name__)
        config = build_config(app)

        self.assertEqual(config["DB_BACKEND"], "postgresql")
        self.assertEqual(
            config["DATABASE_URL_SANITIZED"], "postgresql://admin:***@dbhost:5432/turnodb"
        )
        self.assertEqual(
            app.config["DATABASE_URL"], "postgresql://admin:pass123@dbhost:5432/turnodb"
        )
        self.assertEqual(
            app.config["DATABASE_URL_SANITIZED"], "postgresql://admin:***@dbhost:5432/turnodb"
        )

    def test_create_pg_pool_parameters(self):
        """Verifica la creación del pool con los parámetros de diseño aprobados."""
        conninfo = "postgresql://usr:pwd@localhost:5432/testdb"
        pool = create_pg_pool(
            conninfo,
            min_size=1,
            max_size=10,
            max_idle=300.0,
            max_lifetime=1800.0,
            timeout=10.0,
            open=False,
        )

        try:
            self.assertEqual(pool.min_size, 1)
            self.assertEqual(pool.max_size, 10)
            self.assertEqual(pool.max_idle, 300.0)
            self.assertEqual(pool.max_lifetime, 1800.0)
            self.assertEqual(pool.timeout, 10.0)
        finally:
            pool.close()

    def test_init_and_close_pg_pool_global(self):
        """Verifica el ciclo de vida del pool a nivel global."""
        conninfo = "postgresql://usr:pwd@localhost:5432/testdb"
        pool = init_pg_pool(conninfo=conninfo, open=False)

        self.assertIsNotNone(pool)
        self.assertEqual(get_pg_pool(), pool)

        close_pg_pool(pool)
        self.assertIsNone(get_pg_pool())

    def test_init_and_close_pg_pool_flask_app(self):
        """Verifica el ciclo de vida del pool asociado a una aplicación Flask."""
        app = Flask(__name__)
        app.config["DATABASE_URL"] = "postgresql://usr:pwd@localhost:5432/testdb"
        app.config["DB_BACKEND"] = "postgresql"

        pool = init_pg_pool(app, open=False)
        self.assertIsNotNone(pool)
        self.assertEqual(get_pg_pool(app), pool)

        close_pg_pool(app=app)
        self.assertNotIn("pg_pool", app.extensions)


if __name__ == "__main__":
    unittest.main()
