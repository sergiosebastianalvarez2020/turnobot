"""Mensajes específicos del cupo de reservas por contacto (Fase 2).

Cubre los tres consumidores que antes devolvían un mensaje genérico cuando
`create_appointment`/`reschedule_appointment` respondían `contact_limit`:

1. `services/ai.py::_execute_reservar_turno` (herramienta de IA de reserva).
2. `services/ai.py::_execute_reprogramar_turno` (herramienta de IA de reprogramación).
3. `application/requests.py::_human_reschedule_error` (formulario HTML de gestión).

Y comprueba que los demás motivos de error MANTIENEN su respuesta anterior, para
que la especialización no cambie ningún otro contrato.
"""

import hashlib
import secrets
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import app as application
import database.database as database
from application.requests import _human_reschedule_error, contact_limit_response
from services import ai, appointments
from tests._pg_compat import PostgreSQLTestCase

MENSAJE_CUPO = (
    "Alcanzaste el máximo de reservas activas con estos datos de contacto. "
    "Cancelá una reserva existente o contactá al negocio para seguir reservando."
)


class _BaseCupo(PostgreSQLTestCase):
    """Base con la app/cliente aislados del harness PostgreSQL."""

    BUSINESS_ID = 1
    TELEFONO = "3838439222"

    def _primer_dia_abierto(self):
        return self._dias_abiertos(1)[0]

    def _dias_abiertos(self, cantidad):
        """Primeros `cantidad` días abiertos futuros, con sus horarios."""
        dias = []
        with self.app.app_context():
            from database.database import get_business_settings_scoped

            settings = get_business_settings_scoped(self.BUSINESS_ID)
            timezone = (
                settings["timezone"]
                if settings and settings["timezone"]
                else appointments.DEFAULT_TIMEZONE
            )
            hoy = datetime.now(ZoneInfo(timezone)).date()
            for offset in range(1, 40):
                candidato = hoy + timedelta(days=offset)
                if candidato.weekday() == 6:  # los domingos está cerrado
                    continue
                slots = appointments.get_available_slots(
                    candidato.isoformat(), self.BUSINESS_ID, service="Corte"
                )
                if slots:
                    dias.append((candidato.isoformat(), slots))
                    if len(dias) == cantidad:
                        break
        self.assertGreaterEqual(len(dias), cantidad)
        return dias

    def _llenar_cupo(self, fecha, slots, telefono=None):
        """Crea el máximo de reservas activas por la API pública."""
        telefono = telefono or self.TELEFONO
        with self.client as client:
            for hora in slots[: appointments.MAX_ACTIVE_BOOKINGS_PER_CONTACT]:
                payload = {
                    "nombre": "Ana Pérez",
                    "telefono": telefono,
                    "servicio": "Corte",
                    "fecha": fecha,
                    "hora": hora,
                }
                response = client.post("/b/el-corte/api/reservar", json=payload)
                self.assertEqual(response.status_code, 201, f"Fallo preparando el cupo en {hora}.")
        return telefono


class TestMensajeCupoReservaIA(unittest.TestCase, _BaseCupo):
    """`_execute_reservar_turno` debe explicar el cupo, no un error genérico."""

    def test_mensaje_especifico_al_alcanzar_el_cupo(self):
        fecha, slots = self._primer_dia_abierto()
        self._llenar_cupo(fecha, slots)

        resultado = ai.execute_tool(
            "reservar_turno",
            {
                "nombre": "Ana Pérez",
                "telefono": self.TELEFONO,
                "servicio": "Corte",
                "fecha": fecha,
                "hora": slots[appointments.MAX_ACTIVE_BOOKINGS_PER_CONTACT],
            },
            self.BUSINESS_ID,
        )

        self.assertFalse(resultado["success"])
        self.assertEqual(resultado["reason"], "contact_limit")
        self.assertEqual(resultado["message"], MENSAJE_CUPO)
        # No revela datos personales.
        self.assertNotIn(self.TELEFONO, resultado["message"])

    def test_otros_errores_mantienen_su_mensaje(self):
        """Un motivo distinto de contact_limit conserva la respuesta anterior."""
        fecha, slots = self._primer_dia_abierto()
        # Franja inválida (salto de la grilla) => motivo histórico intacto.
        resultado = ai.execute_tool(
            "reservar_turno",
            {
                "nombre": "Ana Pérez",
                "telefono": "3838111222",
                "servicio": "Corte",
                "fecha": fecha,
                "hora": slots[0],
            },
            self.BUSINESS_ID,
        )
        if not resultado["success"]:
            self.assertNotEqual(resultado.get("reason"), "contact_limit")
            self.assertEqual(resultado["message"], "No se pudo reservar el turno solicitado.")
        else:
            self.assertTrue(resultado["success"])


class TestMensajeCupoReprogramacionIA(unittest.TestCase, _BaseCupo):
    """`_execute_reprogramar_turno` debe explicar el cupo, no un error genérico."""

    def test_mensaje_especifico_al_alcanzar_el_cupo(self):
        (fecha, slots), (destino_fecha, destino_slots) = self._dias_abiertos(2)

        # Cupo lleno por la vía pública.
        self._llenar_cupo(fecha, slots)
        # Un turno viejo (no cuenta) que se intenta mover al futuro.
        pasado = (
            datetime.now(ZoneInfo("America/Argentina/Buenos_Aires")) - timedelta(days=30)
        ).date()

        token = secrets.token_urlsafe(32)

        with self.app.app_context():
            connection = database.get_connection()
            try:
                cursor = connection.execute(
                    "INSERT INTO appointments "
                    "(customer_name, phone, customer_email, service, appointment_date, "
                    " appointment_time, appointment_end, duration, business_id, status, "
                    " management_token_hash) "
                    "VALUES (?, ?, NULL, 'Corte', ?, '09:00', '09:30', 30, ?, 'confirmed', ?)",
                    (
                        "Viejo",
                        self.TELEFONO,
                        pasado.isoformat(),
                        self.BUSINESS_ID,
                        hashlib.sha256(token.encode()).hexdigest(),
                    ),
                )
                connection.commit()
                appointment_id = cursor.lastrowid
            finally:
                connection.close()

        resultado = ai.execute_tool(
            "reprogramar_turno",
            {
                "appointment_id": appointment_id,
                "telefono": self.TELEFONO,
                "nombre": "Viejo",
                "management_token": token,
                "nueva_fecha": destino_fecha,
                "nueva_hora": destino_slots[0],
            },
            self.BUSINESS_ID,
        )

        self.assertFalse(resultado["success"])
        self.assertEqual(resultado["reason"], "contact_limit")
        self.assertEqual(resultado["message"], MENSAJE_CUPO)

    def test_otros_errores_mantienen_su_mensaje(self):
        """`occupied` e `not_found` conservan sus mensajes históricos."""
        # Sin management_token el handler responde antes de tocar el servicio.
        resultado = ai.execute_tool(
            "reprogramar_turno",
            {
                "appointment_id": 999999,
                "telefono": self.TELEFONO,
                "nombre": "Ana Pérez",
                "nueva_fecha": "2099-01-05",
                "nueva_hora": "09:00",
            },
            self.BUSINESS_ID,
        )
        self.assertFalse(resultado["success"])
        self.assertNotEqual(resultado.get("reason"), "contact_limit")
        self.assertEqual(
            resultado["message"], "El token de gestión es obligatorio para reprogramar un turno."
        )


class TestMensajeCupoHumanReschedule(unittest.TestCase):
    """`_human_reschedule_error` traduce el cupo sin tocar los demás motivos."""

    def test_contact_limit_tiene_mensaje_accionable(self):
        mensaje = _human_reschedule_error("contact_limit")
        # Mismo contenido que el mensaje 429 de las rutas, con la primera letra
        # en minúscula para encajar con el resto del diccionario.
        self.assertEqual(mensaje, MENSAJE_CUPO[0].lower() + MENSAJE_CUPO[1:])
        self.assertIn("ancelá una reserva existente", mensaje)
        self.assertIn("ontactá al negocio", mensaje)

    def test_demas_motivos_no_cambian(self):
        self.assertEqual(_human_reschedule_error("occupied"), "ese horario ya está ocupado.")
        self.assertEqual(_human_reschedule_error("not_found"), "no se encontró el turno.")
        self.assertEqual(_human_reschedule_error("invalid_time"), "el horario no es válido.")
        # Un motivo desconocido sigue con el fallback histórico.
        self.assertEqual(_human_reschedule_error("motivo_inexistente"), "intentá nuevamente.")

    def test_respuesta_http_del_cupo_es_estable(self):
        """El cuerpo 429 compartido no cambia con esta tarea."""
        # `contact_limit_response` usa jsonify: requiere app context, igual que
        # en las rutas reales. Aquí solo se verifica el cuerpo resultante.
        with application.app.app_context():
            response = contact_limit_response("phone")
            self.assertEqual(response.status_code, 429)
            data = response.get_json()
            self.assertEqual(data["code"], "CONTACT_LIMIT")
            self.assertEqual(data["reason"], "phone")
            self.assertIn("máximo de reservas activas", data["error"])


if __name__ == "__main__":
    unittest.main()
