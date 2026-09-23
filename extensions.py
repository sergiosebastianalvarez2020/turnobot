"""
Módulo de extensiones compartidas: rate limiting, CSRF, etc.
Estado global y helpers puros, sin dependencias de Flask app.
"""

import secrets
import threading
from collections import defaultdict, deque
from time import monotonic

from flask import session

# ============================================================
# RATE LIMITING - Estado global y helpers
# ============================================================

# Límite máximo de claves distintas en el diccionario de rate limiting
RATE_LIMIT_MAX_KEYS = 10_000

# Ventana de tiempo en segundos para rate limiting (sliding window)
RATE_LIMIT_WINDOW_SECONDS = 60

# Estado global del rate limiting: { key -> deque[timestamps] }
rate_limit_state = defaultdict(deque)

# Lock para acceso concurrente al estado de rate limiting
_RATE_LIMIT_LOCK = threading.Lock()


def _prune_rate_limit_state(now=None, max_keys=RATE_LIMIT_MAX_KEYS):
    """Elimina claves inactivas y acota el estado de rate limiting.

    - Descarta los timestamps más antiguos que la ventana de 60s.
    - Elimina las claves que quedaron vacías.
    - Si el estado supera max_keys, expulsa por FIFO (timestamp más antiguo)
      hasta quedar acotado.
    Se ejecuta bajo _RATE_LIMIT_LOCK; se toman snapshots para no modificar el
    diccionario mientras se itera.
    """
    with _RATE_LIMIT_LOCK:
        if now is None:
            now = monotonic()
        for key in list(rate_limit_state.keys()):
            requests = rate_limit_state.get(key)
            if requests is None:
                continue
            while requests and now - requests[0] > RATE_LIMIT_WINDOW_SECONDS:
                requests.popleft()
            if not requests:
                del rate_limit_state[key]
        if len(rate_limit_state) > max_keys:
            oldest_by_key = []
            for key, requests in rate_limit_state.items():
                if requests:
                    oldest_by_key.append((requests[0], key))
            oldest_by_key.sort()
            to_drop = len(rate_limit_state) - max_keys
            for _, key in oldest_by_key[:to_drop]:
                if key in rate_limit_state:
                    del rate_limit_state[key]


def _is_request_allowed(key, limit):
    """Verifica si una request está permitida según el rate limit.

    Args:
        key: Clave única para el contador (ej. "chat:ip:1.2.3.4:business:1")
        limit: Número máximo de requests permitidas en la ventana

    Returns:
        True si la request está permitida, False si se supera el límite
    """
    if len(rate_limit_state) > RATE_LIMIT_MAX_KEYS:
        _prune_rate_limit_state()
    with _RATE_LIMIT_LOCK:
        now = monotonic()
        requests = rate_limit_state[key]
        while requests and now - requests[0] > RATE_LIMIT_WINDOW_SECONDS:
            requests.popleft()
        if len(requests) >= limit:
            return False
        requests.append(now)
        return True


# ============================================================
# CSRF - Helpers
# ============================================================


def csrf_token():
    """Genera y retorna un token CSRF único para la sesión actual.

    Si no existe token en la sesión, genera uno nuevo usando secrets.token_urlsafe(32).
    El token se almacena en session['csrf_token'].

    Returns:
        str: Token CSRF para la sesión actual
    """
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def valid_csrf_token(value):
    """Valida un token CSRF contra el token de la sesión.

    Usa secrets.compare_digest para evitar ataques de timing.

    Args:
        value: Token CSRF a validar (proveniente del formulario)

    Returns:
        bool: True si el token es válido, False en caso contrario
    """
    return bool(value) and secrets.compare_digest(value, session.get("csrf_token", ""))
