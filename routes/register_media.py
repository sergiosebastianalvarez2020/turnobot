"""Registro de rutas de media (uploads de negocio + proxy de imágenes)."""

from routes.media import serve_business_media, serve_external_image


def register(app):
    """Registra las rutas de media."""
    app.add_url_rule(
        "/media/business_<path:filename>",
        endpoint="media_business",
        view_func=serve_business_media,
        methods=["GET"],
    )
    app.add_url_rule(
        "/media/external",
        endpoint="media_external",
        view_func=serve_external_image,
        methods=["GET"],
    )
