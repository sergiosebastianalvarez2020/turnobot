"""
Blueprint para endpoint de healthcheck.
"""

from flask import Blueprint, jsonify
import logging

health_bp = Blueprint("health", __name__)
logger = logging.getLogger(__name__)


@health_bp.route("/health", methods=["GET"], endpoint="health")
def health():
    """Endpoint de healthcheck para monitoreo.

    Verifica conectividad a la base de datos.

    Returns:
        JSON con status 'ok'/'error' y estado de base de datos.
    """
    connection = None
    try:
        from app import get_connection
        connection = get_connection()
        connection.execute("SELECT 1").fetchone()
        return jsonify({"status": "ok", "database": "ok"}), 200
    except Exception:
        logging.getLogger(__name__).exception("Health check failed")
        return jsonify({"status": "error", "database": "error"}), 503
    finally:
        if connection is not None:
            connection.close()