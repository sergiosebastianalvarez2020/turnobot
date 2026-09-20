"""Seguridad HTTP global: headers, límite de payload y manejadores de error
(Bloque 5B, Paso 6).

add_security_headers, _reject_oversized_requests y los error handlers
extraídos de app.py, manteniendo lógica idéntica.
"""

import os
from time import monotonic

from flask import abort, current_app, g, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

from application.logging_config import logger

_ERROR_MESSAGES = {
    400: "Solicitud inválida.",
    403: "Acceso prohibido.",
    404: "Recurso no encontrado.",
    405: "Método no permitido.",
    429: "Demasiadas solicitudes. Intentá nuevamente en unos momentos.",
    413: "Solicitud demasiado grande.",
    500: "Error interno del servidor.",
}


def _is_json_request():
    return (
        request.is_json
        or request.path.startswith("/api/")
        or "/api/" in request.path
        or (request.headers.get("Accept", "") and "json" in request.headers.get("Accept", ""))
    )


def _json_error(code, message):
    return jsonify({
        "success": False,
        "error": message,
        "code": code,
        "request_id": getattr(g, "request_id", None) or "-",
    })


def _html_error(code, message):
    return render_template(
        "error.html",
        code=code,
        message=message,
        request_id=getattr(g, "request_id", None) or "-",
    ), code


def handle_400(error):
    message = getattr(error, "description", None) or _ERROR_MESSAGES[400]
    if _is_json_request():
        return _json_error("BAD_REQUEST", message), 400
    return _html_error(400, message)


def handle_403(error):
    message = getattr(error, "description", None) or _ERROR_MESSAGES[403]
    if _is_json_request():
        return _json_error("FORBIDDEN", message), 403
    return _html_error(403, message)


def handle_404(error):
    message = getattr(error, "description", None) or _ERROR_MESSAGES[404]
    if not message or "The requested URL was not found" in message:
        message = _ERROR_MESSAGES[404]
    if _is_json_request():
        return _json_error("NOT_FOUND", message), 404
    return _html_error(404, message)


def handle_405(error):
    message = "Método no permitido."
    if _is_json_request():
        return _json_error("METHOD_NOT_ALLOWED", message), 405
    return _html_error(405, message)


def handle_413(error):
    message = _ERROR_MESSAGES[413]
    if _is_json_request():
        return _json_error("PAYLOAD_TOO_LARGE", message), 413
    return _html_error(413, message)


def handle_429(error):
    message = _ERROR_MESSAGES[429]
    if _is_json_request():
        return _json_error("RATE_LIMITED", message), 429
    return _html_error(429, message)


def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    request_id = getattr(g, "request_id", None)
    if request_id:
        response.headers.setdefault("X-Request-ID", request_id)
    if os.getenv("FLASK_ENV") == "production":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")

    # Correlación de petición HTTP (omitir /static/ para evitar ruido)
    if not request.path.startswith("/static/"):
        start_time = getattr(g, "start_time", None)
        latency_ms = round((monotonic() - start_time) * 1000, 2) if start_time else 0.0
        logger.info(
            "HTTP %s %s -> %s (%sms)",
            request.method,
            request.path,
            response.status_code,
            latency_ms,
            extra={
                "status_code": response.status_code,
                "latency_ms": latency_ms,
            },
        )
    return response


def _reject_oversized_requests():
    max_length = current_app.config.get("MAX_CONTENT_LENGTH")
    if max_length and (request.content_length or 0) > max_length:
        abort(413)


def handle_500(error):
    logger.exception("Error 500: %s", getattr(error, "description", str(error)))
    message = _ERROR_MESSAGES[500]
    if _is_json_request():
        return _json_error("INTERNAL_ERROR", message), 500
    return _html_error(500, message)


def handle_unhandled_exception(error):
    if isinstance(error, HTTPException):
        return error
    logger.exception("Excepción no controlada: %s", error)
    message = _ERROR_MESSAGES[500]
    if _is_json_request():
        return _json_error("INTERNAL_ERROR", message), 500
    return _html_error(500, message)