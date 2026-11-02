"""Crea la cuenta SUPERADMIN de la plataforma (CLI seguro).

Uso:
    python scripts/create_superadmin.py --email admin@tu-dominio.com

La contraseña se solicita de forma oculta (getpass). NO se imprime.
Devuelve el enlace de acceso: /superadmin/login
"""

import argparse
import importlib
import sys
from getpass import getpass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from services import platform  # noqa: E402


def _require_postgresql_backend():
    from database.pg_pool import get_database_backend

    load_dotenv(dotenv_path=PROJECT_ROOT / ".env")
    backend = get_database_backend()
    if backend != "postgresql":
        raise RuntimeError("La configuración de base de datos debe ser PostgreSQL")


def _close_initialized_pool(flask_app, previous_pool):
    from database.pg_pool import close_pg_pool, get_pg_pool

    if flask_app is not None:
        pool = flask_app.extensions.get("pg_pool")
        if pool is not None:
            close_pg_pool(app=flask_app)
            return

    pool = get_pg_pool()
    if pool is not None and pool is not previous_pool:
        close_pg_pool(pool=pool)


def _create_superadmin(email, password):
    """Crea el SUPERADMIN con el pool de la aplicación y lo cierra al terminar."""
    from database.pg_pool import get_pg_pool

    _require_postgresql_backend()
    previous_pool = get_pg_pool()
    flask_app = None

    try:
        application = importlib.import_module("app")
        flask_app = application.app
        if flask_app.config.get("DB_BACKEND") != "postgresql":
            raise RuntimeError("La configuración de base de datos debe ser PostgreSQL")
        if flask_app.extensions.get("pg_pool") is None:
            raise RuntimeError("La aplicación no inicializó el pool PostgreSQL")

        result = platform.create_superadmin(email, password, email)
    except BaseException as original_error:
        try:
            _close_initialized_pool(flask_app, previous_pool)
        except BaseException as cleanup_error:
            original_error.add_note(
                "La limpieza del pool también falló "
                f"({type(cleanup_error).__name__}); se conserva la excepción original."
            )
        raise

    _close_initialized_pool(flask_app, previous_pool)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Crear SUPERADMIN de la plataforma")
    parser.add_argument("--email", required=True, help="Email del superadmin")
    args = parser.parse_args(argv)

    _require_postgresql_backend()
    password = getpass("Contraseña del superadmin: ")
    if len(password) < 12:
        print("La contraseña debe tener al menos 12 caracteres.")
        return 1

    result = _create_superadmin(args.email, password)
    if not result["success"]:
        print(f"No se pudo crear: {result['reason']}")
        return 1
    print("Superadmin creado. Login: /superadmin/login")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
