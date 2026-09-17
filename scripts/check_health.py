"""Healthcheck HTTP de disponibilidad de TurnoBot (P.8-F2).

Consulta ``http://127.0.0.1:5000/health`` (o la URL indicada con ``--url``)
y devuelve:

    * exit 0  si el endpoint responde HTTP 200;
    * exit 1  si se agotan los intentos sin obtener HTTP 200.

Evita falsos positivos durante reinicios normales: usa reintentos con
intervalo mayor que ``RestartSec`` del servicio (5 s por defecto).

Solo stdlib. NO lee ni imprime credenciales SMTP, ``/etc/turnobot.env`` ni
ningún secreto; nunca imprime el cuerpo de la respuesta.

Uso:
    python scripts/check_health.py [--url ...] [--attempts 3] [--timeout 5] [--delay 10]
"""

import argparse
import sys
import time
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:5000/health"
DEFAULT_TIMEOUT = 5
DEFAULT_ATTEMPTS = 3
DEFAULT_DELAY = 10
EXPECTED_STATUS = 200

# Nombres de variables/verbos sensibles que nunca deben aparecer en la salida.
SENSITIVE_MARKERS = (
    "SMTP",
    "PASSWORD",
    "PASSWD",
    "SECRET",
    "TOKEN",
    "API_KEY",
    "AUTHORIZATION",
    "X-API",
)


def sanitize(text):
    """Enmascara marcadores sensibles en un mensaje (defensivo)."""
    for marker in SENSITIVE_MARKERS:
        if marker in text:
            text = text.replace(marker, "***")
    return text


def probe(url, timeout):
    """GET a ``url`` con timeout.

    Devuelve ``(codigo, motivo)`` donde ``motivo`` es la descripción del
    error cuando no hubo respuesta (o una cadena vacía en caso contrario).
    Nunca devuelve ni imprime el cuerpo de la respuesta.
    """
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, ""
    except urllib.error.HTTPError as error:
        return error.code, ""
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return None, sanitize(str(error))
    except Exception as error:  # noqa: BLE001 - defensivo, mensaje sanitizado
        return None, sanitize(type(error).__name__)


def check_health(url=DEFAULT_URL, attempts=DEFAULT_ATTEMPTS,
                 timeout=DEFAULT_TIMEOUT, delay=DEFAULT_DELAY):
    """Realiza hasta ``attempts`` intentos separados por ``delay`` segundos.

    Devuelve ``(ok, intentos)`` donde ``intentos`` es una lista de códigos
    obtenidos (``None`` = sin respuesta HTTP).
    """
    resultados = []
    for attempt in range(1, attempts + 1):
        code, _ = probe(url, timeout)
        resultados.append(code)
        if code == EXPECTED_STATUS:
            return True, resultados
        if attempt < attempts:
            time.sleep(delay)
    return False, resultados


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Healthcheck HTTP de disponibilidad de TurnoBot."
    )
    parser.add_argument("--url", default=DEFAULT_URL,
                        help=f"URL a consultar (por defecto {DEFAULT_URL}).")
    parser.add_argument("--attempts", type=int, default=DEFAULT_ATTEMPTS,
                        help=f"Número de intentos (por defecto {DEFAULT_ATTEMPTS}).")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help=f"Timeout por intento en segundos (por defecto {DEFAULT_TIMEOUT}).")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                        help=f"Intervalo entre intentos en segundos (por defecto {DEFAULT_DELAY}).")
    args = parser.parse_args(argv)

    if args.attempts < 1:
        print("ERROR: --attempts debe ser >= 1", file=sys.stderr)
        return 2
    if args.timeout < 0:
        print("ERROR: --timeout no puede ser negativo", file=sys.stderr)
        return 2
    if args.delay < 0:
        print("ERROR: --delay no puede ser negativo", file=sys.stderr)
        return 2

    ok, intentos = check_health(
        url=args.url, attempts=args.attempts,
        timeout=args.timeout, delay=args.delay,
    )

    if ok:
        print(f"OK: {args.url} respondio HTTP {EXPECTED_STATUS}")
        return 0

    print(
        f"FALLO: {args.url} no respondio HTTP {EXPECTED_STATUS} "
        f"tras {args.attempts} intento(s).",
        file=sys.stderr,
    )
    for index, code in enumerate(intentos, start=1):
        if code is None:
            print(f"  intento {index}: sin respuesta HTTP", file=sys.stderr)
        else:
            print(f"  intento {index}: HTTP {code}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())