"""
Módulo de rutas públicas: landing page y contexto de negocio.

Contiene las view functions para:
- /          → endpoint: index
- /b/<slug>  → endpoint: business_index

Estas rutas NO usan Blueprint registrado (para preservar endpoint names
globales sin prefijo) y NO consumen el mecanismo de name='' que está
reservado para auth_bp.

La resolución de tenant (load_current_business) se mantiene en app.py
como before_request hook.
"""

from flask import abort, g, render_template


def index():
    """Landing page principal.

    Returns:
        Renderizado de index.html con configuración pública del frontend.
    """
    import app as application

    business_id = application.get_current_business_id()
    settings = (
        application.get_business_settings_scoped(business_id)
        if business_id is not None
        else application.get_business_settings()
    )
    return render_template(
        "index.html",
        public_frontend_config=application.build_public_frontend_config(settings),
    )


def business_index(slug):
    """Landing page para un negocio específico vía slug.

    Args:
        slug: Slug del negocio desde request.view_args.

    Returns:
        Renderizado de index.html con configuración pública del frontend.
    """
    import app as application

    business = g.current_business
    if business is None or business.get("slug") != slug:
        abort(404)

    business_id = application.get_current_business_id()
    settings = (
        application.get_business_settings_scoped(business_id)
        if business_id is not None
        else application.get_business_settings()
    )
    return render_template(
        "index.html",
        public_frontend_config=application.build_public_frontend_config(settings),
    )
