"""Fase 1 anti-abuso de reservas: honeypot, tiempo mínimo de envío y cupo por contacto.

Cubre:
- El flujo normal de reserva (wizard y API) sigue funcionando.
- El honeypot rechaza envíos automatizados sin afectar formularios legítimos.
- El tiempo mínimo de llenado resiste timestamps manipulados y no bloquea
  reintentos ni flujos sin formulario renderizado.
- El cupo de turnos activos por teléfono/email (medido sobre la base) bloquea
  el relleno de agenda, usa valores normalizados y aísla tenants.
- Una solicitud bloqueada no crea turnos.
- Los reintentos idempotentes conservan el comportamiento existente.
- La compatibilidad con SQLite (mismo motor de reservas, sin configuración extra).

La suite funcional corre contra PostgreSQL aislado por test (ver conftest.py);
la clase `TestCupoContactoSqlite` corre contra una base SQLite temporal para
verificar el mismo camino sobre el backend que usa producción.
"""

import hashlib
import os
import re
import secrets
import tempfile
import threading
import time
import unittest
import unittest.mock
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import app as application
import database.database as database
from services import appointments
from tests._pg_compat import PostgreSQLTestCase

BUSINESS_SLUG = "el-corte"
BUSINESS_ID = 1


class _CasoFuncional(unittest.TestCase, PostgreSQLTestCase):
    """Base unittest + app/client aislados por el harness PostgreSQL."""

    BUSINESS_ID = BUSINESS_ID
    BUSINESS_SLUG = BUSINESS_SLUG

    def setUp(self):
        application.rate_limit_state.clear()

    def _maximo(self):
        return appointments.MAX_ACTIVE_BOOKINGS_PER_CONTACT

    # ---------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------

    def _primer_dia_abierto(self):
        """(fecha, [horarios]) del primer día con horarios disponibles."""
        with self.app.app_context():
            from database.database import get_business_settings_scoped

            settings = get_business_settings_scoped(self.BUSINESS_ID)
            timezone = (
                settings["timezone"]
                if settings and settings["timezone"]
                else appointments.DEFAULT_TIMEZONE
            )
            today = datetime.now(ZoneInfo(timezone)).date()
            for offset in range(1, 20):
                candidate = today + timedelta(days=offset)
                # El seed cierra los domingos; se evita el domingo explícitamente
                # para no depender del día de ejecución.
                if candidate.weekday() == 6:
                    continue
                slots = appointments.get_available_slots(
                    candidate.isoformat(), self.BUSINESS_ID, service="Corte"
                )
                if slots:
                    return candidate.isoformat(), slots
        raise AssertionError("No hay días abiertos con horarios en los próximos 20 días.")

    def _payload_reserva(self, fecha, hora, telefono="3838439222", **extra):
        payload = {
            "nombre": "Ana Pérez",
            "telefono": telefono,
            "servicio": "Corte",
            "fecha": fecha,
            "hora": hora,
        }
        payload.update(extra)
        return payload

    def _reservar_api(self, client, payload):
        return client.post(f"/b/{self.BUSINESS_SLUG}/api/reservar", json=payload)

    def _cantidad_turnos(self, telefono=None):
        from database.database import get_connection

        with self.app.app_context():
            connection = get_connection()
            try:
                if telefono:
                    return connection.execute(
                        "SELECT COUNT(*) AS n FROM appointments WHERE business_id = ? "
                        "AND phone = ?",
                        (self.BUSINESS_ID, appointments.normalize_phone(telefono)),
                    ).fetchone()["n"]
                return connection.execute(
                    "SELECT COUNT(*) AS n FROM appointments WHERE business_id = ?",
                    (self.BUSINESS_ID,),
                ).fetchone()["n"]
            finally:
                connection.close()

    def _cancelar(self, client, appointment_id, telefono, management_token):
        return client.post(
            f"/b/{self.BUSINESS_SLUG}/api/cancelar",
            json={
                "appointment_id": appointment_id,
                "telefono": telefono,
                "management_token": management_token,
                "customer_name": "Ana Pérez",
            },
        )

    def _abrir_paso3(self, client, fecha, hora):
        """GET del paso 3: devuelve (csrf, nombre_honeypot, form_rendered_at)."""
        page = client.get(
            f"/b/{self.BUSINESS_SLUG}/reservar/datos?servicio=Corte&fecha={fecha}&hora={hora}"
        )
        self.assertEqual(page.status_code, 200)
        csrf_match = re.search(r'name="csrf_token"\s+value="([^"]+)"', page.text)
        self.assertIsNotNone(csrf_match, "El paso 3 debe exponer el token CSRF.")
        honeypot_matches = re.findall(r'<input type="hidden" name="(hp_[^"]+)"', page.text)
        self.assertEqual(len(honeypot_matches), 1, "El paso 3 debe tener un único honeypot.")
        rendered_match = re.search(r'name="form_rendered_at"[^>]*value="([^"]+)"', page.text)
        self.assertIsNotNone(rendered_match)
        return (csrf_match.group(1), honeypot_matches[0], rendered_match.group(1))

    def _envejecer_formulario(self, client, segundos=30):
        """Simula (sin dormir) que el formulario se renderizó hace `segundos`.

        El valor lo fija el SERVIDOR en la sesión; acá solo se adelanta ese
        registro para probar el camino de "pasó tiempo suficiente" de forma
        determinista. El cliente nunca gana tiempo por un valor que invente.
        """
        rendered = int(time.time()) - segundos
        with client.session_transaction() as sess:
            sess["reserva_form_rendered_at"] = [rendered]
        return str(rendered)

    def _reservar_wizard(self, client, fecha, hora, telefono="3838439222", **extra):
        csrf, honeypot_field, _rendered_at = self._abrir_paso3(client, fecha, hora)
        # Un usuario real tarda más de MIN_FORM_SECONDS en confirmar: se simula
        # ese tiempo envejeciendo el timestamp que el servidor tiene registrado.
        rendered_at = self._envejecer_formulario(client)
        payload = {
            "csrf_token": csrf,
            "nombre": "Ana Pérez",
            "telefono": telefono,
            "servicio": "Corte",
            "fecha": fecha,
            "hora": hora,
            "idempotency_key": f"test-{time.time_ns()}",
            honeypot_field: "",
            "form_rendered_at": rendered_at,
        }
        payload.update(extra)
        return client.post(f"/b/{self.BUSINESS_SLUG}/reservar/confirmar", json=payload)


class TestFlujoNormalSigueFuncionando(_CasoFuncional):
    def test_reserva_api_ok(self):
        fecha, slots = self._primer_dia_abierto()
        with self.client as client:
            response = self._reservar_api(client, self._payload_reserva(fecha, slots[0]))

        self.assertEqual(response.status_code, 201)
        data = response.get_json()
        self.assertTrue(data["success"])
        self.assertIn("appointment_id", data)
        self.assertTrue(data["management_token"])
        self.assertEqual(self._cantidad_turnos(), 1)

    def test_reserva_wizard_ok(self):
        fecha, slots = self._primer_dia_abierto()
        with self.client as client:
            response = self._reservar_wizard(client, fecha, slots[0])

        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.get_json()["success"])
        self.assertEqual(self._cantidad_turnos(), 1)

    def test_reserva_sin_email_ok(self):
        """El email sigue siendo opcional: el cupo se mide también por teléfono."""
        fecha, slots = self._primer_dia_abierto()
        with self.client as client:
            response = self._reservar_api(
                client, self._payload_reserva(fecha, slots[0], telefono="3838444444")
            )
        self.assertEqual(response.status_code, 201)

    def test_slot_ocupado_sigue_ocupado(self):
        fecha, slots = self._primer_dia_abierto()
        with self.client as client:
            first = self._reservar_api(client, self._payload_reserva(fecha, slots[0]))
            self.assertEqual(first.status_code, 201)
            second = self._reservar_api(
                client, self._payload_reserva(fecha, slots[0], telefono="3838555555")
            )
        self.assertEqual(second.status_code, 400)
        self.assertEqual(second.get_json()["reason"], "occupied")


class TestHoneypot(_CasoFuncional):
    def test_honeypot_disparado_en_api_rechaza_y_no_crea_turno(self):
        fecha, slots = self._primer_dia_abierto()
        with self.client as client:
            response = self._reservar_api(
                client, self._payload_reserva(fecha, slots[0], hp_aleatorio="http://spam.example")
            )

        self.assertEqual(response.status_code, 400)
        data = response.get_json()
        self.assertEqual(data["code"], "HONEYPOT_TRIGGERED")
        self.assertEqual(self._cantidad_turnos(), 0)

    def test_honeypot_disparado_en_wizard_rechaza_y_no_crea_turno(self):
        fecha, slots = self._primer_dia_abierto()
        with self.client as client:
            response = self._reservar_wizard(client, fecha, slots[0], hp_rellenado="bot")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "HONEYPOT_TRIGGERED")
        self.assertEqual(self._cantidad_turnos(), 0)

    def test_honeypot_vacio_no_bloquea(self):
        fecha, slots = self._primer_dia_abierto()
        with self.client as client:
            response = self._reservar_wizard(client, fecha, slots[0])
        self.assertEqual(response.status_code, 201)

    def test_honeypot_con_valor_vacio_o_espacio_no_bloquea(self):
        fecha, slots = self._primer_dia_abierto()
        with self.client as client:
            response = self._reservar_api(
                client, self._payload_reserva(fecha, slots[0], hp_vacio="", hp_espacio="   ")
            )
        self.assertEqual(response.status_code, 201)

    def test_nombre_aleatorio_y_sin_autocompletado(self):
        fecha, slots = self._primer_dia_abierto()
        url = f"/b/{self.BUSINESS_SLUG}/reservar/datos?servicio=Corte&fecha={fecha}&hora={slots[0]}"
        with self.client as client:
            primera = client.get(url)
            segunda = client.get(url)

        self.assertEqual(primera.status_code, 200)
        self.assertEqual(segunda.status_code, 200)

        nombres = [
            re.search(r'<input type="hidden" name="(hp_[^"]+)"', page.text).group(1)
            for page in (primera, segunda)
        ]
        self.assertNotEqual(nombres[0], nombres[1], "El nombre debe variar por render.")
        self.assertRegex(nombres[0], r"^hp_")

        honeypot_input = re.search(
            r'<input type="hidden" name="' + re.escape(nombres[0]) + r'"[^>]*>', primera.text
        )
        self.assertIsNotNone(honeypot_input)
        self.assertIn('autocomplete="off"', honeypot_input.group(0))
        self.assertIn('value=""', honeypot_input.group(0))
        self.assertNotIn("required", honeypot_input.group(0))

    def test_api_sin_campo_honeypot_sigue_reservando(self):
        """Clientes programáticos que no renderizan el formulario no se afectan."""
        fecha, slots = self._primer_dia_abierto()
        with self.client as client:
            response = self._reservar_api(
                client, self._payload_reserva(fecha, slots[0], telefono="3838666666")
            )
        self.assertEqual(response.status_code, 201)


class TestTiempoMinimoDeEnvio(_CasoFuncional):
    def test_envio_inmediato_rechazado(self):
        fecha, slots = self._primer_dia_abierto()
        with self.client as client:
            csrf, honeypot_field, rendered_at = self._abrir_paso3(client, fecha, slots[0])
            response = client.post(
                f"/b/{self.BUSINESS_SLUG}/reservar/confirmar",
                json={
                    "csrf_token": csrf,
                    "nombre": "Ana Pérez",
                    "telefono": "3838439222",
                    "servicio": "Corte",
                    "fecha": fecha,
                    "hora": slots[0],
                    honeypot_field: "",
                    "form_rendered_at": rendered_at,
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "FORM_TOO_FAST")
        self.assertEqual(self._cantidad_turnos(), 0)

    def test_timestamp_inventado_rechazado(self):
        fecha, slots = self._primer_dia_abierto()
        with self.client as client:
            csrf, honeypot_field, _rendered_at = self._abrir_paso3(client, fecha, slots[0])
            response = client.post(
                f"/b/{self.BUSINESS_SLUG}/reservar/confirmar",
                json={
                    "csrf_token": csrf,
                    "nombre": "Ana Pérez",
                    "telefono": "3838439222",
                    "servicio": "Corte",
                    "fecha": fecha,
                    "hora": slots[0],
                    honeypot_field: "",
                    # Válido como entero, pero NO emitido por esta sesión.
                    "form_rendered_at": str(int(time.time()) + 3600),
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "FORM_TOO_FAST")
        self.assertEqual(self._cantidad_turnos(), 0)

    def test_timestamp_ilegible_rechazado(self):
        fecha, slots = self._primer_dia_abierto()
        with self.client as client:
            csrf, honeypot_field, _rendered_at = self._abrir_paso3(client, fecha, slots[0])
            response = client.post(
                f"/b/{self.BUSINESS_SLUG}/reservar/confirmar",
                json={
                    "csrf_token": csrf,
                    "nombre": "Ana Pérez",
                    "telefono": "3838439222",
                    "servicio": "Corte",
                    "fecha": fecha,
                    "hora": slots[0],
                    honeypot_field: "",
                    "form_rendered_at": "no-es-un-timestamp",
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "FORM_TOO_FAST")

    def test_timestamp_ausente_rechazado_cuando_hubo_render(self):
        """El servidor no completa el dato que falta: sin timestamp no hay OK."""
        fecha, slots = self._primer_dia_abierto()
        with self.client as client:
            csrf, honeypot_field, _rendered_at = self._abrir_paso3(client, fecha, slots[0])
            response = client.post(
                f"/b/{self.BUSINESS_SLUG}/reservar/confirmar",
                json={
                    "csrf_token": csrf,
                    "nombre": "Ana Pérez",
                    "telefono": "3838439222",
                    "servicio": "Corte",
                    "fecha": fecha,
                    "hora": slots[0],
                    honeypot_field: "",
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "FORM_TOO_FAST")
        self.assertEqual(self._cantidad_turnos(), 0)

    def test_tiempo_suficiente_acepta(self):
        fecha, slots = self._primer_dia_abierto()
        with self.client as client:
            csrf, honeypot_field, _rendered_at = self._abrir_paso3(client, fecha, slots[0])
            # El render se envejece en la sesión del SERVIDOR: el cliente no gana
            # tiempo por decreto propio, solo se simula que pasó más tiempo real.
            rendered_at = self._envejecer_formulario(client, segundos=30)
            response = client.post(
                f"/b/{self.BUSINESS_SLUG}/reservar/confirmar",
                json={
                    "csrf_token": csrf,
                    "nombre": "Ana Pérez",
                    "telefono": "3838439222",
                    "servicio": "Corte",
                    "fecha": fecha,
                    "hora": slots[0],
                    honeypot_field: "",
                    "form_rendered_at": rendered_at,
                },
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(self._cantidad_turnos(), 1)

    def test_dos_pestanas_abiertas_ambas_aceptadas(self):
        """Ninguna pestaña legítima queda invalidada por haber abierto otra."""
        fecha, slots = self._primer_dia_abierto()
        with self.client as client:
            csrf, honeypot_field, _rendered_a = self._abrir_paso3(client, fecha, slots[0])
            # Un segundo render registra OTRO timestamp en la misma sesión; el
            # primero sigue siendo válido (la lista es acotada, no exclusiva).
            _csrf_b, _honeypot_b, _rendered_b = self._abrir_paso3(client, fecha, slots[0])
            rendered_at = self._envejecer_formulario(client, segundos=30)

            primera = client.post(
                f"/b/{self.BUSINESS_SLUG}/reservar/confirmar",
                json={
                    "csrf_token": csrf,
                    "nombre": "Ana Pérez",
                    "telefono": "3838439222",
                    "servicio": "Corte",
                    "fecha": fecha,
                    "hora": slots[0],
                    honeypot_field: "",
                    "form_rendered_at": rendered_at,
                },
            )
            segunda = client.post(
                f"/b/{self.BUSINESS_SLUG}/reservar/confirmar",
                json={
                    "csrf_token": csrf,
                    "nombre": "Ana Pérez",
                    "telefono": "3838439222",
                    "servicio": "Corte",
                    "fecha": fecha,
                    "hora": slots[0],
                    honeypot_field: "",
                    "form_rendered_at": rendered_at,
                    "idempotency_key": "clave-reintento-legitimo",
                },
            )

        self.assertEqual(primera.status_code, 201)
        # El segundo reintento del mismo formulario no falla por tiempo.
        self.assertEqual(segunda.status_code, 400)
        self.assertNotEqual(segunda.get_json().get("code"), "FORM_TOO_FAST")

    def test_recarga_del_paso4_no_rompe_el_formulario(self):
        """Un refresh o carga directa del paso 4 mantiene el formulario funcional."""
        fecha, slots = self._primer_dia_abierto()
        with self.client as client:
            page = client.get(
                f"/b/{self.BUSINESS_SLUG}/reservar/confirmar"
                f"?servicio=Corte&fecha={fecha}&hora={slots[0]}"
            )
            self.assertEqual(page.status_code, 200)

            csrf = re.search(r'name="csrf_token"\s+value="([^"]+)"', page.text).group(1)
            honeypot_field = re.search(r'<input type="hidden" name="(hp_[^"]+)"', page.text).group(
                1
            )
            rendered_at = self._envejecer_formulario(client, segundos=30)

            response = client.post(
                f"/b/{self.BUSINESS_SLUG}/reservar/confirmar",
                json={
                    "csrf_token": csrf,
                    "nombre": "Ana Pérez",
                    "telefono": "3838989898",
                    "servicio": "Corte",
                    "fecha": fecha,
                    "hora": slots[0],
                    honeypot_field: "",
                    "form_rendered_at": rendered_at,
                },
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(self._cantidad_turnos(telefono="3838989898"), 1)

    def test_api_sin_formulario_renderizado_no_se_bloquea(self):
        """Flujo programático (sin wizard) no exige timestamp de formulario."""
        fecha, slots = self._primer_dia_abierto()
        with self.client as client:
            response = self._reservar_api(
                client, self._payload_reserva(fecha, slots[0], telefono="3838777777")
            )
        self.assertEqual(response.status_code, 201)


class TestCupoPorContacto(_CasoFuncional):
    def test_cupo_bloquea_la_reserva_extra(self):
        maximo = self._maximo()
        fecha, slots = self._primer_dia_abierto()
        horarios = slots[: maximo + 2]
        self.assertGreater(
            len(horarios), maximo, "El día de prueba necesita más horarios que el cupo."
        )

        with self.client as client:
            for hora in horarios[:maximo]:
                response = self._reservar_api(client, self._payload_reserva(fecha, hora))
                self.assertEqual(response.status_code, 201, f"Fallo reservando {hora}")

            bloqueada = self._reservar_api(client, self._payload_reserva(fecha, horarios[maximo]))

        self.assertEqual(bloqueada.status_code, 429)
        data = bloqueada.get_json()
        self.assertEqual(data["code"], "CONTACT_LIMIT")
        self.assertEqual(data["reason"], "phone")
        # No enumera datos personales.
        self.assertNotIn("3838439222", data["error"])
        # La reserva bloqueada NO creó turno: se mantienen exactamente el máximo.
        self.assertEqual(self._cantidad_turnos(), maximo)

    def test_telefono_normalizado_comparte_cupo(self):
        maximo = self._maximo()
        fecha, slots = self._primer_dia_abierto()
        horarios = slots[: maximo + 1]

        with self.client as client:
            for hora in horarios[:maximo]:
                response = self._reservar_api(client, self._payload_reserva(fecha, hora))
                self.assertEqual(response.status_code, 201)

            # Mismo número con otro formato: sigue siendo el mismo contacto.
            bloqueada = self._reservar_api(
                client, self._payload_reserva(fecha, horarios[maximo], telefono="38-3843 9222")
            )

        self.assertEqual(bloqueada.status_code, 429)
        self.assertEqual(bloqueada.get_json()["reason"], "phone")

    def test_email_normalizado_comparte_cupo(self):
        """El email mide su propio cupo: 5 turnos con el mismo mail y teléfonos distintos."""
        maximo = self._maximo()
        fecha, slots = self._primer_dia_abierto()
        horarios = slots[: maximo + 1]
        self.assertGreater(len(slots), maximo, "El día de prueba necesita más horarios.")

        with self.client as client:
            for indice, hora in enumerate(horarios[:maximo]):
                response = self._reservar_api(
                    client,
                    self._payload_reserva(
                        fecha, hora, telefono=f"3838{900000 + indice}", email="Ana@Ejemplo.com"
                    ),
                )
                self.assertEqual(response.status_code, 201, f"Fallo reservando {hora}")

            # Teléfono nuevo (no llega a su propio cupo) pero email ya al máximo,
            # escrito en minúsculas: es el mismo contacto.
            bloqueada = self._reservar_api(
                client,
                self._payload_reserva(
                    fecha, horarios[maximo], telefono="3838888888", email="ana@ejemplo.com"
                ),
            )

        self.assertEqual(bloqueada.status_code, 429)
        self.assertEqual(bloqueada.get_json()["reason"], "email")

    def test_cancelar_libera_cupo(self):
        maximo = self._maximo()
        fecha, slots = self._primer_dia_abierto()
        horarios = slots[: maximo + 1]

        with self.client as client:
            creados = []
            for hora in horarios[:maximo]:
                response = self._reservar_api(client, self._payload_reserva(fecha, hora))
                self.assertEqual(response.status_code, 201)
                creados.append(response.get_json())

            bloqueada = self._reservar_api(client, self._payload_reserva(fecha, horarios[maximo]))
            self.assertEqual(bloqueada.status_code, 429)

            cancelada = self._cancelar(
                client, creados[0]["appointment_id"], "3838439222", creados[0]["management_token"]
            )
            self.assertTrue(cancelada.get_json()["success"])

            liberada = self._reservar_api(client, self._payload_reserva(fecha, horarios[maximo]))

        self.assertEqual(liberada.status_code, 201)

    def test_otro_telefono_no_se_afecta(self):
        maximo = self._maximo()
        fecha, slots = self._primer_dia_abierto()
        horarios = slots[: maximo + 1]

        with self.client as client:
            for hora in horarios[:maximo]:
                response = self._reservar_api(client, self._payload_reserva(fecha, hora))
                self.assertEqual(response.status_code, 201)

            otro = self._reservar_api(
                client, self._payload_reserva(fecha, horarios[maximo], telefono="3838000000")
            )

        self.assertEqual(otro.status_code, 201)

    def test_aislamiento_entre_tenants(self):
        """El cupo se mide con el business_id del servidor, nunca con el del cliente."""
        maximo = self._maximo()
        fecha, slots = self._primer_dia_abierto()

        with self.client as client:
            for hora in slots[:maximo]:
                response = self._reservar_api(client, self._payload_reserva(fecha, hora))
                self.assertEqual(response.status_code, 201)

            # El payload miente con business_id=2: el cupo sigue midiéndose en el
            # negocio resuelto por el servidor (el-corte).
            bloqueada = self._reservar_api(
                client,
                self._payload_reserva(fecha, slots[maximo], telefono="3838439222", business_id=2),
            )

        self.assertEqual(bloqueada.status_code, 429)

        # Y otro negocio tiene su propio cupo: el contacto no está cerca del máximo.
        with self.app.app_context():
            from database.database import get_connection

            connection = get_connection()
            try:
                existe_otro = connection.execute(
                    "SELECT COUNT(*) AS n FROM appointments WHERE business_id = ?", (2,)
                ).fetchone()["n"]
            finally:
                connection.close()
        self.assertEqual(existe_otro, 0)

        resultado_otro_tenant = appointments.get_contact_booking_limit(
            "3838439222", None, business_id=2
        )
        self.assertIsNone(resultado_otro_tenant)

    def test_reintento_idempotente_no_consume_cupo_extra(self):
        """Un reintento del mismo formulario devuelve el turno original."""
        fecha, slots = self._primer_dia_abierto()
        with self.client as client:
            clave = "clave-idempotente-fase1"
            primera = self._reservar_api(
                client, self._payload_reserva(fecha, slots[0], idempotency_key=clave)
            )
            self.assertEqual(primera.status_code, 201)

            reintento = self._reservar_api(
                client, self._payload_reserva(fecha, slots[0], idempotency_key=clave)
            )

        self.assertEqual(reintento.status_code, 200)
        data = reintento.get_json()
        self.assertTrue(data["idempotent_replay"])
        self.assertEqual(data["appointment_id"], primera.get_json()["appointment_id"])
        self.assertEqual(self._cantidad_turnos(), 1)


class TestCupoReprogramacion(_CasoFuncional):
    """Reprogramar no debe permitir pasar del cupo ni romper el caso normal."""

    TELEFONO = "3838439222"

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

    def _insertar_confirmado(self, fecha_str, telefono, hora="09:00", nombre="Viejo"):
        """Inserta un turno confirmado por SQL (la API rechaza fechas pasadas).

        Genera su propio `management_token` y guarda solo el SHA-256, igual que
        la aplicación: así el test conoce el secreto para llamar a la API.
        """
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self.app.app_context():
            from database.database import get_connection

            connection = get_connection()
            try:
                cursor = connection.execute(
                    "INSERT INTO appointments "
                    "(customer_name, phone, customer_email, service, appointment_date, "
                    " appointment_time, appointment_end, duration, business_id, status, "
                    " management_token_hash) "
                    "VALUES (?, ?, NULL, 'Corte', ?, ?, ?, 30, ?, 'confirmed', ?)",
                    (
                        nombre,
                        appointments.normalize_phone(telefono),
                        fecha_str,
                        hora,
                        "09:30",
                        self.BUSINESS_ID,
                        token_hash,
                    ),
                )
                connection.commit()
                appointment_id = cursor.lastrowid
            finally:
                connection.close()
        return appointment_id, token

    def test_reprogramar_uno_de_los_turnos_sigue_permitido(self):
        """El caso normal: mover uno de los turnos que ya se tienen."""
        (fecha, slots), (destino, slots_destino) = self._dias_abiertos(2)

        with self.client as client:
            creada = self._reservar_api(
                client, self._payload_reserva(fecha, slots[2], telefono=self.TELEFONO)
            )
            self.assertEqual(creada.status_code, 201)
            data = creada.get_json()

            respuesta = client.post(
                f"/b/{self.BUSINESS_SLUG}/api/reprogramar",
                json={
                    "appointment_id": data["appointment_id"],
                    "management_token": data["management_token"],
                    "customer_name": "Ana Pérez",
                    "telefono": self.TELEFONO,
                    "nueva_fecha": destino,
                    "nueva_hora": slots_destino[0],
                },
            )

        self.assertEqual(respuesta.status_code, 200)
        self.assertTrue(respuesta.get_json()["success"])

    def test_reprogramar_no_supera_el_cupo(self):
        """Con el cupo lleno, mover una reserva vieja a una fecha futura NO pasa."""
        maximo = self._maximo()
        fecha, slots = self._primer_dia_abierto()
        pasado = (
            datetime.now(ZoneInfo("America/Argentina/Buenos_Aires")) - timedelta(days=30)
        ).date()

        with self.client as client:
            for hora in slots[:maximo]:
                alta = self._reservar_api(
                    client, self._payload_reserva(fecha, hora, telefono=self.TELEFONO)
                )
                self.assertEqual(alta.status_code, 201)

            # Un turno viejo confirmado que hoy no ocupa la agenda.
            appointment_id, token = self._insertar_confirmado(
                pasado.isoformat(), self.TELEFONO, nombre="Viejo"
            )

            respuesta = client.post(
                f"/b/{self.BUSINESS_SLUG}/api/reprogramar",
                json={
                    "appointment_id": appointment_id,
                    "management_token": token,
                    "customer_name": "Viejo",
                    "telefono": self.TELEFONO,
                    "nueva_fecha": fecha,
                    "nueva_hora": slots[maximo],
                },
            )

        self.assertEqual(respuesta.status_code, 429)
        data = respuesta.get_json()
        self.assertEqual(data["code"], "CONTACT_LIMIT")
        self.assertEqual(data["reason"], "phone")
        # El turno viejo no se movió: sigue en su fecha pasada.
        with self.app.app_context():
            from database.database import get_connection

            connection = get_connection()
            try:
                fila = connection.execute(
                    "SELECT appointment_date FROM appointments WHERE id = ?", (appointment_id,)
                ).fetchone()
            finally:
                connection.close()
        self.assertEqual(str(fila["appointment_date"]), pasado.isoformat())

    def test_turno_futuro_cancelado_no_suma_al_cupo(self):
        """`status='confirmed'` es la condición: un futuro cancelado no cuenta."""
        maximo = self._maximo()
        fecha, slots = self._primer_dia_abierto()

        with self.client as client:
            creada = self._reservar_api(
                client, self._payload_reserva(fecha, slots[0], telefono=self.TELEFONO)
            )
            self.assertEqual(creada.status_code, 201)
            data = creada.get_json()

            # Se cancela por SQL: la API de cancelación valida otros factores y
            # acá solo interesa el estado para el conteo.
            from database.database import get_connection

            with self.app.app_context():
                connection = get_connection()
                try:
                    connection.execute(
                        "UPDATE appointments SET status = 'cancelled' WHERE id = ?",
                        (data["appointment_id"],),
                    )
                    connection.commit()
                finally:
                    connection.close()

            for hora in slots[1 : maximo + 1]:
                alta = self._reservar_api(
                    client, self._payload_reserva(fecha, hora, telefono=self.TELEFONO)
                )
                self.assertEqual(alta.status_code, 201, f"Fallo reservando {hora}")


class TestFalloDeBackendNoDesactivaLaProteccion(_CasoFuncional):
    def test_error_en_el_cupo_no_permite_reservar(self):
        """Si el chequeo de cupo falla, la reserva NO se crea en silencio."""
        fecha, slots = self._primer_dia_abierto()

        with unittest.mock.patch(
            "routes.booking_abuse.get_contact_booking_limit",
            side_effect=RuntimeError("backend caído"),
        ):
            with self.client as client:
                response = self._reservar_api(
                    client, self._payload_reserva(fecha, slots[0], telefono="3838111111")
                )

        self.assertNotEqual(response.status_code, 201)
        self.assertEqual(self._cantidad_turnos(telefono="3838111111"), 0)

    def test_error_en_el_cupo_del_wizard_no_permite_reservar(self):
        """Un fallo del chequeo de cupo no crea la reserva en silencio.

        El timestamp envejecido en la sesión TIENE que ser el que envía el
        formulario: si son distintos, la ruta corta por tiempo antes de llegar al
        cupo y el test pasaría sin ejercitar nada (fue el defecto que motivó
        esta corrección).
        """
        fecha, slots = self._primer_dia_abierto()
        with self.client as client:
            csrf, honeypot_field, _rendered_at = self._abrir_paso3(client, fecha, slots[0])
            rendered_at = self._envejecer_formulario(client, segundos=30)
            with unittest.mock.patch(
                "routes.booking_abuse.get_contact_booking_limit",
                side_effect=RuntimeError("backend caído"),
            ):
                response = client.post(
                    f"/b/{self.BUSINESS_SLUG}/reservar/confirmar",
                    json={
                        "csrf_token": csrf,
                        "nombre": "Ana Pérez",
                        "telefono": "3838122222",
                        "servicio": "Corte",
                        "fecha": fecha,
                        "hora": slots[0],
                        honeypot_field: "",
                        "form_rendered_at": rendered_at,
                    },
                )

        self.assertNotEqual(response.status_code, 201)
        self.assertEqual(self._cantidad_turnos(telefono="3838122222"), 0)

    def test_cupo_en_wizard_devuelve_429_contact_limit(self):
        """El camino del wizard también bloquea por cupo con el código esperado."""
        maximo = self._maximo()
        fecha, slots = self._primer_dia_abierto()
        self.assertGreater(len(slots), maximo)

        with self.client as client:
            for hora in slots[:maximo]:
                alta = self._reservar_api(client, self._payload_reserva(fecha, hora))
                self.assertEqual(alta.status_code, 201, f"Fallo reservando {hora}")

            csrf, honeypot_field, _rendered_at = self._abrir_paso3(client, fecha, slots[maximo])
            rendered_at = self._envejecer_formulario(client, segundos=30)

            response = client.post(
                f"/b/{self.BUSINESS_SLUG}/reservar/confirmar",
                json={
                    "csrf_token": csrf,
                    "nombre": "Ana Pérez",
                    "telefono": "3838439222",
                    "servicio": "Corte",
                    "fecha": fecha,
                    "hora": slots[maximo],
                    honeypot_field: "",
                    "form_rendered_at": rendered_at,
                },
            )

        self.assertEqual(response.status_code, 429)
        data = response.get_json()
        self.assertEqual(data["code"], "CONTACT_LIMIT")
        self.assertEqual(data["reason"], "phone")
        self.assertEqual(self._cantidad_turnos(), maximo)


class TestSinRegresionesDeFlujo(_CasoFuncional):
    def test_disponibilidad_sigue_disponible(self):
        fecha, slots = self._primer_dia_abierto()
        with self.client as client:
            response = client.get(
                f"/b/{self.BUSINESS_SLUG}/api/disponibilidad/{fecha}?servicio=Corte"
            )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["success"])
        self.assertTrue(data["horarios_disponibles"])

    def test_consultar_turnos_con_token_sigue_funcionando(self):
        fecha, slots = self._primer_dia_abierto()
        with self.client as client:
            creada = self._reservar_api(client, self._payload_reserva(fecha, slots[0]))
            data = creada.get_json()
            consulta = client.get(
                f"/b/{self.BUSINESS_SLUG}/api/turnos"
                f"?nombre=Ana+P%C3%A9rez&telefono=3838439222"
                f"&management_token={data['management_token']}"
            )
        self.assertEqual(consulta.status_code, 200)
        self.assertEqual(len(consulta.get_json()["turnos"]), 1)

    def test_cupo_permite_el_maximo_esperado(self):
        maximo = self._maximo()
        fecha, slots = self._primer_dia_abierto()
        with self.client as client:
            codigos = [
                self._reservar_api(client, self._payload_reserva(fecha, hora)).status_code
                for hora in slots[:maximo]
            ]
        self.assertEqual(codigos, [201] * maximo)


class TestCupoContactoSqlite(unittest.TestCase):
    """Misma medición de cupo contra el backend SQLite (una base temporal).

    No toca la base real: `DATABASE_PATH` se redirige a un archivo temporal y se
    fuerza `DB_BACKEND=sqlite` durante el test, igual que los tests de migración.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "appointments.db"
        self._original_path = os.environ.get("DATABASE_PATH")
        self._original_backend = os.environ.get("DB_BACKEND")
        self._original_url = os.environ.get("DATABASE_URL")

        import database.database as database

        self._original_db_path = database.DATABASE_PATH
        database.DATABASE_PATH = self.database_path
        os.environ["DB_BACKEND"] = "sqlite"
        os.environ.pop("DATABASE_URL", None)

        database.init_database()
        self._sembrar()

    def tearDown(self):
        import database.database as database

        database.DATABASE_PATH = self._original_db_path
        for clave, valor in (
            ("DB_BACKEND", self._original_backend),
            ("DATABASE_URL", self._original_url),
        ):
            if valor is None:
                os.environ.pop(clave, None)
            else:
                os.environ[clave] = valor
        self.temp_dir.cleanup()

    def _sembrar(self):
        import database.database as database

        # migration 003 ya siembra el negocio 1 (slug 'el-corte'): no se inserta.
        hoy = date.today()
        self.fecha_futura = (hoy + timedelta(days=1)).isoformat()
        self.fecha_pasada = (hoy - timedelta(days=30)).isoformat()

        connection = database.get_connection()
        try:
            for indice in range(appointments.MAX_ACTIVE_BOOKINGS_PER_CONTACT + 1):
                connection.execute(
                    "INSERT INTO appointments "
                    "(customer_name, phone, customer_email, service, appointment_date, "
                    " appointment_time, appointment_end, duration, business_id, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'confirmed')",
                    (
                        f"Cliente {indice}",
                        "3838439222",
                        "ana@ejemplo.com",
                        "Corte",
                        self.fecha_futura,
                        f"{9 + indice:02d}:00",
                        f"{9 + indice:02d}:30",
                        30,
                        1,
                    ),
                )
            connection.commit()
        finally:
            connection.close()

    def test_cupo_alcanzado_devuelve_phone(self):
        self.assertEqual(appointments.get_contact_booking_limit("3838439222", None, 1), "phone")

    def test_cupo_alcanzado_devuelve_email(self):
        self.assertEqual(
            appointments.get_contact_booking_limit(None, "ana@ejemplo.com", 1), "email"
        )

    def test_email_insensible_a_mayusculas(self):
        self.assertEqual(
            appointments.get_contact_booking_limit(None, "ANA@EJEMPLO.COM", 1), "email"
        )

    def test_telefono_normalizado(self):
        self.assertEqual(appointments.get_contact_booking_limit("38 (3843) 9222", None, 1), "phone")

    def test_otro_negocio_no_se_afecta(self):
        self.assertIsNone(appointments.get_contact_booking_limit("3838439222", None, 2))

    def test_sin_datos_no_evalua(self):
        self.assertIsNone(appointments.get_contact_booking_limit(None, None, 1))
        self.assertIsNone(appointments.get_contact_booking_limit("", "", 1))

    def test_limite_personalizado(self):
        """El cupo es parametrizable: con 6 turnos activos, un límite de 7 deja pasar."""
        self.assertIsNone(
            appointments.get_contact_booking_limit(
                "3838439222", None, 1, limit=appointments.MAX_ACTIVE_BOOKINGS_PER_CONTACT + 2
            )
        )

    def test_solo_cuentan_turnos_futuros(self):
        """El historial pasado no bloquea: los turnos viejos quedan cancelados."""
        import database.database as database

        connection = database.get_connection()
        try:
            connection.execute("UPDATE appointments SET status = 'cancelled' WHERE business_id = 1")
            connection.commit()
        finally:
            connection.close()

        self.assertIsNone(appointments.get_contact_booking_limit("3838439222", None, 1))

    def test_solo_cuentan_turnos_confirmados(self):
        import database.database as database

        connection = database.get_connection()
        try:
            # Un turno pasado (no confirmado futuro) no suma.
            connection.execute(
                "INSERT INTO appointments "
                "(customer_name, phone, customer_email, service, appointment_date, "
                " appointment_time, appointment_end, duration, business_id, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'cancelled')",
                (
                    "Histórico",
                    "3838111222",
                    "hist@ejemplo.com",
                    "Corte",
                    self.fecha_pasada,
                    "09:00",
                    "09:30",
                    30,
                    1,
                ),
            )
            connection.commit()
        finally:
            connection.close()

        self.assertIsNone(appointments.get_contact_booking_limit("3838111222", None, 1))

    def test_fecha_pasada_confirmada_no_cuenta(self):
        """Un turno confirmado de fecha pasada (no cancelado) tampoco bloquea."""
        import database.database as database

        connection = database.get_connection()
        try:
            connection.execute(
                "INSERT INTO appointments "
                "(customer_name, phone, customer_email, service, appointment_date, "
                " appointment_time, appointment_end, duration, business_id, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'confirmed')",
                (
                    "Viejo",
                    "3838333444",
                    "viejo@ejemplo.com",
                    "Corte",
                    self.fecha_pasada,
                    "09:00",
                    "09:30",
                    30,
                    1,
                ),
            )
            connection.commit()
        finally:
            connection.close()

        self.assertIsNone(appointments.get_contact_booking_limit("3838333444", None, 1))


class TestCupoConcurrenciaSqlite(unittest.TestCase):
    """El cupo debe respetarse con solicitudes simultáneas (SQLite real).

    Reproduce el defecto de la revisión: N requests que completan el chequeo
    antes de que empiecen las escrituras. La barrera sincroniza justo antes de
    `create_appointment`, que es el interleaving que produce una ráfaga real.

    El enforcement ahora vive DENTRO de la transacción (`BEGIN IMMEDIATE`), así
    que el resultado debe ser exactamente el cupo, sin importar el interleaving.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "race.db"
        self._original_db_path = database.DATABASE_PATH
        self._original_backend = os.environ.get("DB_BACKEND")
        self._original_url = os.environ.get("DATABASE_URL")

        database.DATABASE_PATH = self.database_path
        os.environ["DB_BACKEND"] = "sqlite"
        os.environ.pop("DATABASE_URL", None)

        database.init_database()
        self._sembrar()

    def tearDown(self):
        database.DATABASE_PATH = self._original_db_path
        for clave, valor in (
            ("DB_BACKEND", self._original_backend),
            ("DATABASE_URL", self._original_url),
        ):
            if valor is None:
                os.environ.pop(clave, None)
            else:
                os.environ[clave] = valor
        self.temp_dir.cleanup()

    def _sembrar(self):
        # migration 003 ya siembra el negocio 1 (slug 'el-corte').
        connection = database.get_connection()
        try:
            connection.execute(
                "INSERT OR IGNORE INTO services (name, price, duration, active, business_id) "
                "VALUES ('Corte', 100, 30, 1, 1)"
            )
            for dow in range(7):
                connection.execute(
                    "INSERT OR IGNORE INTO weekly_schedules "
                    "(day_of_week, is_open, morning_start, morning_end, "
                    " afternoon_start, afternoon_end, business_id) "
                    "VALUES (?, 1, '09:00', '13:00', '15:00', '20:00', 1)",
                    (dow,),
                )
            connection.commit()
        finally:
            connection.close()

    def test_rafaga_no_supera_el_cupo(self):
        negocio = 1
        telefono = "3838439222"
        maximo = appointments.MAX_ACTIVE_BOOKINGS_PER_CONTACT
        total = maximo + 3

        fecha = (datetime.now(ZoneInfo(appointments.DEFAULT_TIMEZONE)) + timedelta(days=3)).date()

        # Franjas REALES de la grilla: evita que algún hilo falle por franja
        # inválida en lugar de por cupo (que era un falso negativo del test).
        todos = appointments.get_available_slots(fecha.isoformat(), negocio, service="Corte")
        self.assertGreaterEqual(
            len(todos), total, "El día de prueba necesita más franjas que la ráfaga."
        )
        horarios = todos[:total]

        barrera = threading.Barrier(total, timeout=30)
        resultados = [None] * total
        lock = threading.Lock()

        def worker(indice):
            barrera.wait(timeout=30)  # todas las requests llegan juntas
            resultado = appointments.create_appointment(
                customer_name=f"Bot {indice}",
                phone=telefono,
                service="Corte",
                appointment_date=fecha.isoformat(),
                appointment_time=horarios[indice],
                business_id=negocio,
            )
            with lock:
                resultados[indice] = resultado

        hilos = [threading.Thread(target=worker, args=(i,)) for i in range(total)]
        for hilo in hilos:
            hilo.start()
        for hilo in hilos:
            hilo.join(timeout=60)

        creados = [r for r in resultados if r]
        exitos = [r for r in creados if r.get("success")]
        rechazados = [r for r in creados if not r.get("success")]

        self.assertEqual(len(exitos), maximo, "El cupo debe admitir exactamente el máximo.")
        self.assertEqual(len(rechazados), len(horarios) - maximo)
        self.assertTrue(
            all(r.get("reason") == "contact_limit" for r in rechazados),
            "Los rechazados deben ser explícitamente por cupo.",
        )
        self.assertTrue(
            all(r.get("contact_limit_kind") == "phone" for r in rechazados),
            "Debe informarse qué dimensión alcanzó el cupo.",
        )

        # Verificación en la base: nunca más del máximo confirmado.
        connection = database.get_connection()
        try:
            filas = connection.execute(
                "SELECT COUNT(*) AS n FROM appointments "
                "WHERE business_id = ? AND phone = ? AND status = 'confirmed'",
                (negocio, telefono),
            ).fetchone()["n"]
        finally:
            connection.close()
        self.assertEqual(filas, maximo)

        # Los rechazados no dejaron reservas pendientes de ningún tipo.
        self.assertEqual(appointments.get_contact_booking_limit(telefono, None, negocio), "phone")


class TestCupoConcurrenciaAPI(_CasoFuncional):
    """Misma garantía por HTTP, contra PostgreSQL real y con DÍAS DISTINTOS.

    El advisory lock por negocio+día no cruza días distintos: por eso el cupo se
    serializa además por contacto. Para forzar de forma reproducible el
    interleaving adverso (todas las requests pasan el chequeo anticipado antes de
    que ninguna escriba), se sincroniza SOLO el chequeo anticipado de la ruta; el
    enforcement transaccional queda intacto.
    """

    TELEFONO = "3838439222"

    def _dias_abiertos(self, cantidad):
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
                if candidato.weekday() == 6:
                    continue
                slots = appointments.get_available_slots(
                    candidato.isoformat(), self.BUSINESS_ID, service="Corte"
                )
                if slots:
                    dias.append((candidato.isoformat(), slots))
                    if len(dias) == cantidad:
                        break
        self.assertGreaterEqual(len(dias), cantidad, "No hay días abiertos suficientes.")
        return dias

    def test_rafaga_http_no_supera_el_cupo(self):
        maximo = appointments.MAX_ACTIVE_BOOKINGS_PER_CONTACT
        total = maximo + 3
        dias = self._dias_abiertos(3)

        # Las requests se reparten entre DÍAS DISTINTOS a propósito.
        plan = [(fecha, slots[i % len(slots)]) for i, (fecha, slots) in enumerate(dias)]
        while len(plan) < total:
            fecha, slots = dias[len(plan) % len(dias)]
            plan.append((fecha, slots[len(plan) % len(slots)]))
        plan = plan[:total]

        barrera = threading.Barrier(total, timeout=30)
        real = appointments.get_contact_booking_limit

        def precheck_sincronizado(*args, **kwargs):
            barrera.wait()
            return real(*args, **kwargs)

        def worker(fecha, hora):
            client = self.app.test_client()
            return self._reservar_api(
                client, self._payload_reserva(fecha, hora, telefono=self.TELEFONO)
            )

        with unittest.mock.patch(
            "routes.booking_abuse.get_contact_booking_limit", side_effect=precheck_sincronizado
        ):
            with ThreadPoolExecutor(max_workers=total) as pool:
                futuros = [pool.submit(worker, fecha, hora) for fecha, hora in plan]
                respuestas = [futuro.result() for futuro in futuros]

        codigos = [r.status_code for r in respuestas]
        creados = codigos.count(201)
        bloqueados = codigos.count(429)

        self.assertEqual(
            creados, maximo, f"Se crearon {creados} turnos con cupo {maximo}: {codigos}"
        )
        self.assertEqual(bloqueados, total - maximo)

        for respuesta in respuestas:
            if respuesta.status_code == 429:
                data = respuesta.get_json()
                self.assertEqual(data["code"], "CONTACT_LIMIT")
                self.assertIn(data["reason"], ("phone", "email"))
                self.assertNotIn(self.TELEFONO, data["error"])

        # Verificación en la base y ausencia de filas huérfanas.
        self.assertEqual(self._cantidad_turnos(telefono=self.TELEFONO), maximo)
        with self.app.app_context():
            from database.database import get_connection

            connection = get_connection()
            try:
                pendientes = connection.execute(
                    "SELECT COUNT(*) AS n FROM appointments WHERE business_id = ? "
                    "AND status NOT IN ('confirmed', 'cancelled', 'completed', 'no_show')",
                    (self.BUSINESS_ID,),
                ).fetchone()["n"]
            finally:
                connection.close()
        self.assertEqual(pendientes, 0, "Las reservas rechazadas no deben dejar filas.")


if __name__ == "__main__":
    unittest.main()
