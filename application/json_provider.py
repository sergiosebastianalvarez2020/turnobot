"""Proveedor JSON personalizado para Flask que soporta tipos de PostgreSQL (Fase 4F).

Flask 2.2+ usa ``flask.json.provider.DefaultJSONProvider`` como serializador.
El proveedor por defecto no conoce objetos ``datetime.time`` / ``datetime.date``
que psycopg3 devuelve para columnas TIME / DATE, provocando errores en
``jsonify()``. Este proveedor extiende el default añadiendo soporte para esos
tipos, de modo que la capa de API funciona tanto con SQLite (cadenas) como con
PostgreSQL (objetos datetime).
"""

import datetime

from flask.json.provider import DefaultJSONProvider


class PgAwareJSONProvider(DefaultJSONProvider):
    """DefaultJSONProvider con soporte para datetime.time y datetime.date."""

    def default(self, o):
        if isinstance(o, datetime.time):
            return o.isoformat()
        if isinstance(o, datetime.date):
            return o.isoformat()
        return super().default(o)
