"""Utilidades criptográficas de sesión (Bloque 5B).

_hash_session_token y _now_iso se extraen de routes/auth.py para eliminar la
duplicación entre app.py y routes/auth.py, manteniendo idéntica lógica.
"""

import datetime
import hashlib


def _hash_session_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now_iso():
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M:%S")
