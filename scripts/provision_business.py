"""Provisiona un negocio y su owner desde una consola confiable."""
import argparse
from getpass import getpass

from database.database import create_business_with_owner


def main():
    parser = argparse.ArgumentParser(description="Crear negocio y owner")
    parser.add_argument("name")
    parser.add_argument("email")
    parser.add_argument("--password", help="evitar en producción; si se omite se solicita de forma oculta")
    args = parser.parse_args()
    password = args.password or getpass("Contraseña del owner: ")
    result = create_business_with_owner(args.name, args.email, password)
    print(f"Negocio creado: /b/{result['slug']}/login")


if __name__ == "__main__":
    main()
