"""Configuración de logging de la aplicación.

Clases Formatter/Filter y configure_logging() extraídos de app.py (Bloque 5B).

USE_JSON_LOGS se mantiene como atributo de módulo; StructuredFormatter lo lee
dinámicamente desde app (re-export) para preservar compatibilidad con los
monkeypatches existentes (patch.object(application, "USE_JSON_LOGS", True)).
"""

import datetime
import json
import logging
import os
from logging.handlers import RotatingFileHandler

from flask import g, has_request_context, request

USE_JSON_LOGS = os.getenv("LOG_FORMAT", "plain").lower() == "json"


class RequestContextFilter(logging.Filter):
    """Inyecta contexto HTTP en cada registro de log (request_id, business_id, endpoint, method, path)."""

    def filter(self, record):
        if has_request_context():
            try:
                existing_rid = getattr(record, "request_id", None)
                if existing_rid and existing_rid != "-":
                    record.request_id = existing_rid
                else:
                    record.request_id = getattr(g, "request_id", None) or request.headers.get("X-Request-ID", "").strip() or "-"
            except Exception:
                record.request_id = "-"

            try:
                existing_bid = getattr(record, "business_id", None)
                if existing_bid and existing_bid != "-":
                    record.business_id = str(existing_bid)
                else:
                    business_id = getattr(g, "business_id", None)
                    if business_id is None:
                        curr = getattr(g, "current_business", None)
                        if isinstance(curr, dict):
                            business_id = curr.get("id")
                        elif curr is not None:
                            try:
                                business_id = curr["id"]
                            except Exception:
                                business_id = None
                    record.business_id = str(business_id) if business_id is not None else "-"
            except Exception:
                record.business_id = "-"

            try:
                record.endpoint = getattr(g, "endpoint", None) or request.endpoint or "-"
            except Exception:
                record.endpoint = "-"

            try:
                record.method = getattr(g, "method", None) or request.method or "-"
            except Exception:
                record.method = "-"

            try:
                record.path = getattr(g, "path", None) or request.path or "-"
            except Exception:
                record.path = "-"
        else:
            if not hasattr(record, "request_id") or getattr(record, "request_id") is None:
                record.request_id = "-"
            if not hasattr(record, "business_id") or getattr(record, "business_id") is None:
                record.business_id = "-"
            if not hasattr(record, "endpoint") or getattr(record, "endpoint") is None:
                record.endpoint = "-"
            if not hasattr(record, "method") or getattr(record, "method") is None:
                record.method = "-"
            if not hasattr(record, "path") or getattr(record, "path") is None:
                record.path = "-"
        return True


RequestIdFilter = RequestContextFilter


class StructuredFormatter(logging.Formatter):
    """Formatter que emite logs en JSON o texto consistente con contexto enriquecido."""

    def format(self, record):
        try:
            ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

        req_id = getattr(record, "request_id", "-")
        biz_id = getattr(record, "business_id", "-")
        endpoint = getattr(record, "endpoint", "-")
        method = getattr(record, "method", "-")
        path = getattr(record, "path", "-")

        log_data = {
            "timestamp": ts,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": req_id,
            "business_id": biz_id,
            "endpoint": endpoint,
            "method": method,
            "path": path,
        }
        for attr in ("status_code", "latency_ms"):
            val = getattr(record, attr, None)
            if val is not None:
                log_data[attr] = val

        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        from app import USE_JSON_LOGS as use_json_logs

        if use_json_logs:
            return json.dumps(log_data)

        context_part = f"[{req_id}] [b:{biz_id}] [{method} {endpoint}]"
        extra_info = ""
        if hasattr(record, "status_code") or hasattr(record, "latency_ms"):
            status_val = getattr(record, "status_code", "-")
            lat_val = getattr(record, "latency_ms", "-")
            extra_info = f" (status={status_val} lat={lat_val}ms)"
        exc_part = f"\n{self.formatException(record.exc_info)}" if record.exc_info else ""
        return f"{ts} {record.levelname} {record.name} {context_part} {record.getMessage()}{extra_info}{exc_part}"


logger = logging.getLogger("el_corte.web")

_handler_configured = False


def configure_logging():
    """Configura el logging global de la aplicación (idempotente).

    Reproduce el wire-up que vivía en app.py. No duplica handlers si se
    invoca más de una vez.
    """
    global _handler_configured

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s [%(request_id)s] %(message)s",
    )

    if _handler_configured:
        return

    log_dir = os.getenv("LOG_DIR", "logs")
    os.makedirs(log_dir, exist_ok=True)
    file_handler = RotatingFileHandler(
        os.path.join(log_dir, "app.log"), maxBytes=5_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(StructuredFormatter())

    for handler in logging.getLogger().handlers:
        handler.addFilter(RequestContextFilter())
        if USE_JSON_LOGS:
            handler.setFormatter(StructuredFormatter())
    file_handler.addFilter(RequestContextFilter())
    logging.getLogger().addHandler(file_handler)

    _handler_configured = True