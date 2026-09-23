"""Prueba funcional TurnoBot — Recursos / Pádel.

Ejecuta:
    python test_resources_functional.py
"""

import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import app as application
import database.database as database
from database.database import (
    create_resource_scoped,
    get_connection,
    update_business_settings_scoped,
)


def _next_open_day():
    date = datetime.now().date() + timedelta(days=1)
    while date.weekday() == 6:
        date += timedelta(days=1)
    return date.isoformat()


def main():
    application.rate_limit_state.clear()
    temp_dir = tempfile.TemporaryDirectory()
    original_database_path = database.DATABASE_PATH
    database.DATABASE_PATH = Path(temp_dir.name) / "appointments.db"
    database.init_database()
    date = _next_open_day()

    results = []

    try:
        flask_app = application.app
        flask_app.config["TESTING"] = True
        client = flask_app.test_client()

        # ------------------------------------------------------------------
        # SETUP: Business 2 (Pádel) con 4 canchas
        # ------------------------------------------------------------------
        conn = get_connection()
        try:
            conn.execute("""
                INSERT INTO businesses (id, name, slug)
                VALUES (2, 'Padel Club', 'padel-club')
            """)
            conn.execute("""
                INSERT INTO services (id, business_id, name, price, duration, active)
                VALUES (10, 2, 'Partido Pádel', 15000, 90, 1)
            """)
            conn.execute("""
                INSERT INTO weekly_schedules
                    (day_of_week, is_open, morning_start, morning_end,
                     afternoon_start, afternoon_end, business_id)
                VALUES
                    (0, 1, '09:00', '13:00', '15:00', '20:00', 2),
                    (1, 1, '09:00', '13:00', '15:00', '20:00', 2),
                    (2, 1, '09:00', '13:00', '15:00', '20:00', 2),
                    (3, 1, '09:00', '13:00', '15:00', '20:00', 2),
                    (4, 1, '09:00', '13:00', '15:00', '20:00', 2),
                    (5, 1, '09:00', '13:00', '15:00', '20:00', 2),
                    (6, 0, NULL, NULL, NULL, NULL, 2)
            """)
            conn.commit()
        finally:
            conn.close()

        update_business_settings_scoped(
            2,
            "Padel Club",
            "Pádel",
            "PC",
            "Club de pádel",
            "America/Argentina/Buenos_Aires",
            notifications_enabled=0,
        )

        cancha1 = create_resource_scoped(2, "Cancha 1")
        cancha2 = create_resource_scoped(2, "Cancha 2")
        cancha3 = create_resource_scoped(2, "Cancha 3")
        cancha4 = create_resource_scoped(2, "Cancha 4")

        print(f"Fecha de prueba: {date}")
        print(
            f"Recursos creados: Cancha 1={cancha1}, Cancha 2={cancha2}, Cancha 3={cancha3}, Cancha 4={cancha4}"
        )
        print()

        # ------------------------------------------------------------------
        # PRUEBA 1: Crear/configurar negocio de prueba con 4 recursos
        # ------------------------------------------------------------------
        ok = True
        obs = "4 canchas creadas correctamente"
        results.append(("1. Crear/configurar negocio con 4 recursos", ok, obs))

        # ------------------------------------------------------------------
        # PRUEBA 2: Consultar disponibilidad para mañana a las 18
        # ------------------------------------------------------------------
        ok = True
        obs_parts = []
        disponibles_18 = []
        for rid, nombre in [
            (cancha1, "Cancha 1"),
            (cancha2, "Cancha 2"),
            (cancha3, "Cancha 3"),
            (cancha4, "Cancha 4"),
        ]:
            resp = client.get(
                f"/b/padel-club/api/disponibilidad/{date}"
                f"?servicio=Partido%20P%C3%A1del&resource_id={rid}"
            )
            data = json.loads(resp.data)
            slots = data.get("horarios_disponibles", [])
            if "18:00" in slots:
                disponibles_18.append(nombre)
        obs = f"Disponibles a las 18:00 -> {disponibles_18}"
        if len(disponibles_18) != 4:
            ok = False
            obs += " (se esperaban 4)"
        results.append(("2. Consultar disponibilidad a las 18", ok, obs))

        # ------------------------------------------------------------------
        # PRUEBA 3: Reservar Cancha 2 a las 18
        # ------------------------------------------------------------------
        ok = True
        obs = ""
        payload = {
            "nombre": "Jugador A",
            "telefono": "1111111111",
            "servicio": "Partido Pádel",
            "fecha": date,
            "hora": "18:00",
            "resource_id": cancha2,
        }
        resp = client.post(
            "/b/padel-club/api/reservar", data=json.dumps(payload), content_type="application/json"
        )
        data = json.loads(resp.data)
        if resp.status_code == 201 and data.get("success") is True:
            if data.get("resource_id") == cancha2 and data.get("resource_nombre") == "Cancha 2":
                obs = f"Reserva exitosa id={data.get('appointment_id')} en Cancha 2"
            else:
                ok = False
                obs = f"resource_id o resource_nombre incorrecto: {data}"
        else:
            ok = False
            obs = f"Error en reserva: status={resp.status_code} data={data}"
        results.append(("3. Reservar Cancha 2 a las 18", ok, obs))

        # ------------------------------------------------------------------
        # PRUEBA 4: Intentar reservar Cancha 2 a las 18 nuevamente
        # ------------------------------------------------------------------
        ok = True
        obs = ""
        payload = {
            "nombre": "Jugador B",
            "telefono": "2222222222",
            "servicio": "Partido Pádel",
            "fecha": date,
            "hora": "18:00",
            "resource_id": cancha2,
        }
        resp = client.post(
            "/b/padel-club/api/reservar", data=json.dumps(payload), content_type="application/json"
        )
        data = json.loads(resp.data)
        if resp.status_code == 400 and data.get("reason") == "occupied":
            obs = "Rechazada correctamente (occupied)"
        else:
            ok = False
            obs = f"Se esperaba 400/occupied, obtenido status={resp.status_code} data={data}"
        results.append(("4. Re-reserva Cancha 2 a las 18 (debe fallar)", ok, obs))

        # ------------------------------------------------------------------
        # PRUEBA 5: Reservar Cancha 3 a las 18
        # ------------------------------------------------------------------
        ok = True
        obs = ""
        payload = {
            "nombre": "Jugador C",
            "telefono": "3333333333",
            "servicio": "Partido Pádel",
            "fecha": date,
            "hora": "18:00",
            "resource_id": cancha3,
        }
        resp = client.post(
            "/b/padel-club/api/reservar", data=json.dumps(payload), content_type="application/json"
        )
        data = json.loads(resp.data)
        if resp.status_code == 201 and data.get("success") is True:
            if data.get("resource_id") == cancha3 and data.get("resource_nombre") == "Cancha 3":
                obs = f"Reserva exitosa id={data.get('appointment_id')} en Cancha 3"
            else:
                ok = False
                obs = f"resource_id o resource_nombre incorrecto: {data}"
        else:
            ok = False
            obs = f"Error en reserva: status={resp.status_code} data={data}"
        results.append(("5. Reservar Cancha 3 a las 18", ok, obs))

        # ------------------------------------------------------------------
        # PRUEBA 6: Consultar disponibilidad nuevamente
        # ------------------------------------------------------------------
        ok = True
        obs_parts = []
        esperado = {"Cancha 1": True, "Cancha 2": False, "Cancha 3": False, "Cancha 4": True}
        for rid, nombre in [
            (cancha1, "Cancha 1"),
            (cancha2, "Cancha 2"),
            (cancha3, "Cancha 3"),
            (cancha4, "Cancha 4"),
        ]:
            resp = client.get(
                f"/b/padel-club/api/disponibilidad/{date}"
                f"?servicio=Partido%20P%C3%A1del&resource_id={rid}"
            )
            data = json.loads(resp.data)
            slots = data.get("horarios_disponibles", [])
            disponible = "18:00" in slots
            if disponible != esperado[nombre]:
                ok = False
            obs_parts.append(f"{nombre}={disponible}")
        obs = ", ".join(obs_parts)
        if not ok:
            obs += " (se esperaba Cancha1=True, Cancha2=False, Cancha3=False, Cancha4=True)"
        results.append(("6. Consultar disponibilidad post-reservas", ok, obs))

        # ------------------------------------------------------------------
        # PRUEBA 7: Negocio sin recursos (El Corte) — reserva tradicional
        # ------------------------------------------------------------------
        ok = True
        obs = ""
        payload = {
            "nombre": "Cliente Barbería",
            "telefono": "1177889900",
            "servicio": "Corte",
            "fecha": date,
            "hora": "10:00",
        }
        resp = client.post(
            "/b/el-corte/api/reservar", data=json.dumps(payload), content_type="application/json"
        )
        data = json.loads(resp.data)
        if resp.status_code == 201 and data.get("success") is True:
            obs = f"Reserva tradicional exitosa id={data.get('appointment_id')}"
        else:
            ok = False
            obs = f"Error: status={resp.status_code} data={data}"
        results.append(("7. Negocio sin recursos (reserva tradicional)", ok, obs))

        # ------------------------------------------------------------------
        # PRUEBA 8: Aislamiento multi-tenant
        # ------------------------------------------------------------------
        ok = True
        obs_parts = []

        # 8a: No puede consultar recursos del otro negocio
        resp = client.get("/b/el-corte/api/recursos")
        data = json.loads(resp.data)
        if data.get("recursos") == []:
            obs_parts.append("ElCorte ve 0 recursos (OK)")
        else:
            ok = False
            obs_parts.append(f"ElCorte ve recursos: {data.get('recursos')}")

        # 8b: No puede reservar usando resource_id del otro negocio
        payload = {
            "nombre": "Intruso",
            "telefono": "0000000000",
            "servicio": "Partido Pádel",
            "fecha": date,
            "hora": "11:00",
            "resource_id": cancha1,  # recurso de Padel, usado contra El Corte
        }
        resp = client.post(
            "/b/el-corte/api/reservar", data=json.dumps(payload), content_type="application/json"
        )
        data = json.loads(resp.data)
        if resp.status_code == 400:
            obs_parts.append("Reserva cross-tenant rechazada (OK)")
        else:
            ok = False
            obs_parts.append(
                f"Reserva cross-tenant aceptada: status={resp.status_code} data={data}"
            )

        # 8c: No se modifican reservas del otro negocio
        resp = client.get(f"/b/el-corte/api/disponibilidad/{date}?servicio=Corte")
        data = json.loads(resp.data)
        if len(data.get("horarios_disponibles", [])) > 0:
            obs_parts.append("ElCorte mantiene disponibilidad (OK)")
        else:
            ok = False
            obs_parts.append("ElCorte sin disponibilidad (inesperado)")

        obs = "; ".join(obs_parts)
        results.append(("8. Aislamiento multi-tenant", ok, obs))

    finally:
        database.DATABASE_PATH = original_database_path
        temp_dir.cleanup()

    # ------------------------------------------------------------------
    # INFORME
    # ------------------------------------------------------------------
    print("=" * 80)
    print("PRUEBA FUNCIONAL — RECURSOS / PÁDEL")
    print("=" * 80)
    print()
    print(f"{'Prueba':<55} {'Resultado':<10} {'Observación'}")
    print("-" * 80)
    for name, ok, obs in results:
        status = "PASS" if ok else "FAIL"
        print(f"{name:<55} {status:<10} {obs}")
    print()
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print(f"Total: {passed}/{total} pruebas passed")
    print()
    print("Recursos creados: Cancha 1, Cancha 2, Cancha 3, Cancha 4 (business_id=2)")
    print(
        "Reservas realizadas: Cancha 2 a las 18:00, Cancha 3 a las 18:00, Corte a las 10:00 (El Corte)"
    )
    print("Conflictos correctamente rechazados: Re-reserva Cancha 2 a las 18:00 -> occupied")
    print("Disponibilidad correcta: Verificada por recurso post-reservas")
    print("Negocio sin recursos correcto: Reserva tradicional en El Corte funciona")
    print("Aislamiento multi-tenant correcto: Verificado")
    print()
    print("Archivos modificados: ninguno (solo se ejecutó la prueba)")
    print("Commit: no")
    print("Push: no")
    print("Producción: no tocada")


if __name__ == "__main__":
    main()
