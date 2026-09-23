"""
Módulo de rutas públicas: landing page, wizard de reserva y contexto de negocio.

Contiene las view functions para:
- /                    → endpoint: index
- /b/<slug>            → endpoint: business_index
- /b/<slug>/reservar   → endpoint: business_reservar_wizard (paso 1: servicio)
- /b/<slug>/reservar/fecha  → endpoint: business_reservar_wizard_fecha (paso 2: calendario)
- /b/<slug>/reservar/datos  → endpoint: business_reservar_wizard_datos (paso 3: formulario)
- /b/<slug>/reservar/confirmar → endpoint: business_reservar_wizard_confirmar (paso 4: confirmación)

Estas rutas NO usan Blueprint registrado (para preservar endpoint names
globales sin prefijo) y NO consumen el mecanismo de name='' que está
reservado para auth_bp.

La resolución de tenant (load_current_business) se mantiene en app.py
como before_request hook.
"""

from flask import abort, g, jsonify, render_template, request


def index():
    """Landing page principal (fallback sin tenant)."""
    import app as application

    business_id = application.get_current_business_id()
    settings = (
        application.get_business_settings_scoped(business_id)
        if business_id is not None
        else application.get_business_settings()
    )
    return render_template(
        "index.html", public_frontend_config=application.build_public_frontend_config(settings)
    )


def business_index(slug):
    """Landing page comercial para un negocio específico vía slug.

    Muestra hero, servicios en grid, información del negocio y CTA a wizard de reserva.

    Args:
        slug: Slug del negocio desde request.view_args.

    Returns:
        Renderizado de landing.html con configuración del negocio.
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
    services = application.get_active_services_scoped(business_id) if business_id else []

    return render_template(
        "landing.html",
        business=business,
        services=services,
        public_frontend_config=application.build_public_frontend_config(settings),
    )


def business_reservar_wizard(slug):
    """Paso 1 del wizard: selección de servicio.

    Args:
        slug: Slug del negocio desde request.view_args.

    Returns:
        Renderizado de reservar_wizard.html con step=1.
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
    services = application.get_active_services_scoped(business_id) if business_id else []

    return render_template(
        "reservar_wizard.html",
        business=business,
        services=services,
        step=1,
        public_frontend_config=application.build_public_frontend_config(settings),
    )


def business_reservar_wizard_fecha(slug):
    """Paso 2 del wizard: selección de fecha y horario.

    Args:
        slug: Slug del negocio desde request.view_args.

    Returns:
        Renderizado de reservar_wizard.html con step=2.
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

    # Servicio seleccionado viene como query param
    servicio = request.args.get("servicio", "").strip()
    services = application.get_active_services_scoped(business_id) if business_id else []
    selected_service = next((s for s in services if s["name"] == servicio), None)

    if not selected_service:
        # Redirigir al paso 1 si no hay servicio válido
        return render_template(
            "reservar_wizard.html",
            business=business,
            services=services,
            step=1,
            error="Seleccioná un servicio para continuar.",
            public_frontend_config=application.build_public_frontend_config(settings),
        )

    return render_template(
        "reservar_wizard.html",
        business=business,
        services=services,
        selected_service=selected_service,
        step=2,
        public_frontend_config=application.build_public_frontend_config(settings),
    )


def business_reservar_wizard_datos(slug):
    """Paso 3 del wizard: formulario de datos del cliente.

    Args:
        slug: Slug del negocio desde request.view_args.

    Returns:
        Renderizado de reservar_wizard.html con step=3.
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

    # Parámetros del paso anterior
    servicio = request.args.get("servicio", "").strip()
    fecha = request.args.get("fecha", "").strip()
    hora = request.args.get("hora", "").strip()
    resource_id = request.args.get("resource_id", "").strip()

    services = application.get_active_services_scoped(business_id) if business_id else []
    selected_service = next((s for s in services if s["name"] == servicio), None)

    if not selected_service or not fecha or not hora:
        # Redirigir al paso anterior si faltan datos
        return render_template(
            "reservar_wizard.html",
            business=business,
            services=services,
            step=2,
            error="Seleccioná fecha y horario para continuar.",
            public_frontend_config=application.build_public_frontend_config(settings),
        )

    return render_template(
        "reservar_wizard.html",
        business=business,
        services=services,
        selected_service=selected_service,
        selected_fecha=fecha,
        selected_hora=hora,
        selected_resource_id=resource_id or None,
        step=3,
        public_frontend_config=application.build_public_frontend_config(settings),
    )


def business_reservar_wizard_confirmar(slug):
    """Paso 4 del wizard: confirmación y envío de reserva.

    POST: Crea la reserva vía API interna.
    GET: Muestra resumen final antes de confirmar.

    Args:
        slug: Slug del negocio desde request.view_args.

    Returns:
        GET: Renderizado de reservar_wizard.html con step=4 (resumen).
        POST: JSON response con resultado de la reserva.
    """
    import app as application
    from app import json_object

    business = g.current_business
    if business is None or business.get("slug") != slug:
        abort(404)

    business_id = application.get_current_business_id()
    settings = (
        application.get_business_settings_scoped(business_id)
        if business_id is not None
        else application.get_business_settings()
    )

    if request.method == "POST":
        data = json_object()
        if data is None:
            return jsonify({"success": False, "error": "El cuerpo JSON no es válido."}), 400

        from app import get_client_ip, is_api_request_allowed
        from extensions import RATE_LIMIT_WINDOW_SECONDS

        if not is_api_request_allowed(get_client_ip(), "api:reservar", business_id):
            response = jsonify(
                {
                    "success": False,
                    "code": "RATE_LIMITED",
                    "error": "rate_limit_exceeded",
                    "message": "Demasiadas solicitudes. Esperá un momento.",
                }
            )
            response.status_code = 429
            response.headers["Retry-After"] = str(RATE_LIMIT_WINDOW_SECONDS)
            return response

        from extensions import valid_csrf_token

        if not valid_csrf_token(data.get("csrf_token")):
            return jsonify({"success": False, "error": "Solicitud no válida"}), 400

        # Reutilizar la lógica de reserva existente
        from database.database import get_active_services_scoped
        from services.appointments import create_appointment, validate_email
        from services.notifications import (
            notifications_enabled,
            send_business_confirmation_email,
            send_confirmation_email,
        )

        nombre = data.get("nombre", "").strip()
        telefono = data.get("telefono", "").strip()
        email = data.get("email", "").strip()
        servicio = data.get("servicio", "").strip()
        fecha = data.get("fecha", "").strip()
        hora = data.get("hora", "").strip()
        resource_id_raw = data.get("resource_id")

        # Validaciones básicas
        if not nombre or len(nombre) < 2:
            return jsonify(
                {
                    "success": False,
                    "error": "El nombre y apellido son obligatorios (mínimo 2 caracteres).",
                }
            ), 400
        if not telefono:
            return jsonify({"success": False, "error": "El teléfono es obligatorio."}), 400
        if not telefono.isdigit() or len(telefono) < 7:
            return jsonify(
                {"success": False, "error": "El teléfono debe tener al menos 7 dígitos."}
            ), 400
        if email and not validate_email(email):
            return jsonify({"success": False, "error": "El email no es válido."}), 400

        # Validar servicio
        allowed_services = (
            {row["name"] for row in get_active_services_scoped(business_id)}
            if business_id
            else set()
        )
        if servicio not in allowed_services:
            return jsonify(
                {"success": False, "error": "El servicio seleccionado no es válido."}
            ), 400

        # Validar email obligatorio si notificaciones activas
        if not email and notifications_enabled(business_id):
            return jsonify(
                {
                    "success": False,
                    "code": "email_required",
                    "reason": "email_required",
                    "error": "El email es obligatorio para poder enviarte la confirmación del turno.",
                }
            ), 400

        # Resource ID opcional
        resource_id = None
        if resource_id_raw is not None:
            try:
                resource_id = int(resource_id_raw)
                if resource_id <= 0:
                    resource_id = None
            except (ValueError, TypeError):
                return jsonify({"success": False, "error": "resource_id inválido."}), 400

        # Crear la reserva
        resultado = create_appointment(
            customer_name=nombre,
            phone=telefono,
            service=servicio,
            appointment_date=fecha,
            appointment_time=hora,
            business_id=business_id,
            email=email or None,
            resource_id=resource_id,
        )

        if not resultado.get("success"):
            reason = resultado.get("reason")
            code_map = {
                "past_date": ("past_date", "No podés reservar una fecha que ya pasó."),
                "closed_day": ("closed_day", "Ese día estamos cerrados."),
                "invalid_date": ("invalid_date", "La fecha seleccionada no es válida."),
                "invalid_time": ("invalid_time", "El horario seleccionado no es válido."),
                "occupied": ("occupied", "Ese horario ya está ocupado."),
                "past_time": ("past_time", "Ese horario ya pasó."),
                "invalid_service": (
                    "invalid_service",
                    "El servicio seleccionado no está disponible.",
                ),
                "invalid_resource": (
                    "invalid_resource",
                    "El recurso seleccionado no es válido o no está disponible.",
                ),
                "invalid_name": (
                    "invalid_name",
                    "El nombre y apellido deben tener al menos 2 caracteres.",
                ),
                "invalid_phone": ("invalid_phone", "El teléfono debe tener al menos 7 dígitos."),
            }
            code, msg = code_map.get(reason, ("INTERNAL_ERROR", "No se pudo realizar la reserva."))
            return jsonify({"success": False, "code": code, "reason": reason, "error": msg}), 400

        # Enviar confirmaciones (no bloqueante)
        try:
            current = getattr(g, "current_business", None) or {}
            slug_val = current.get("slug")
            base_url = request.url_root.rstrip("/")
            appointment_data = {
                "id": resultado.get("appointment_id"),
                "customer_name": resultado.get("customer_name"),
                "customer_email": resultado.get("customer_email"),
                "service": resultado.get("service"),
                "appointment_date": resultado.get("appointment_date"),
                "appointment_time": resultado.get("appointment_time"),
                "appointment_end": resultado.get("appointment_end"),
            }
            send_confirmation_email(
                business_id,
                appointment_data,
                management_token=resultado.get("management_token"),
                slug=slug_val,
                public_base_url=base_url,
            )
            send_business_confirmation_email(business_id, appointment_data)
        except Exception:
            application.logger.exception("Error enviando confirmación de turno")

        response_data = {
            "success": True,
            "appointment_id": resultado.get("appointment_id"),
            "management_token": resultado.get("management_token"),
            "message": "El turno fue reservado correctamente.",
        }
        if resultado.get("resource_id"):
            response_data["resource_id"] = resultado["resource_id"]
            from database.database import get_resource_scoped

            resource = get_resource_scoped(resultado["resource_id"], business_id)
            if resource:
                response_data["resource_nombre"] = resource["name"]

        return jsonify(response_data), 201

    # GET: Mostrar resumen final
    servicio = request.args.get("servicio", "").strip()
    fecha = request.args.get("fecha", "").strip()
    hora = request.args.get("hora", "").strip()
    resource_id = request.args.get("resource_id", "").strip()

    services = application.get_active_services_scoped(business_id) if business_id else []
    selected_service = next((s for s in services if s["name"] == servicio), None)

    return render_template(
        "reservar_wizard.html",
        business=business,
        services=services,
        selected_service=selected_service,
        selected_fecha=fecha,
        selected_hora=hora,
        selected_resource_id=resource_id or None,
        step=4,
        public_frontend_config=application.build_public_frontend_config(settings),
    )
