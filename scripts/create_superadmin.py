"""Crea la cuenta SUPERADMIN de la plataforma (CLI seguro).

Uso:
    python scripts/create_superadmin.py --email admin@tu-dominio.com

La contraseña se solicita de forma oculta (getpass). NO se imprime.
Devuelve el enlace de acceso: /superadmin/login
"""

import argparse
from getpass import getpass

from services import platform


def main():
    parser = argparse.ArgumentParser(description="Crear SUPERADMIN de la plataforma")
    parser.add_argument("--email", required=True, help="Email del superadmin")
    parser.add_argument(
        "--password", help="evitar en producción; si se omite se solicita de forma oculta"
    )
    args = parser.parse_args()

    password = args.password or getpass("Contraseña del superadmin: ")
    if not args.password and len(password) < 12:
        print("La contraseña debe tener al menos 12 caracteres.")
        return 1

    result = platform.create_superadmin(args.email, password, args.email)
    if not result["success"]:
        print(f"No se pudo crear: {result['reason']}")
        return 1
    print("Superadmin creado. Login: /superadmin/login")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
