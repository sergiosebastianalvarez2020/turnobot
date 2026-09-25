"""Escenarios concurrentes F.5 contra PostgreSQL REAL (Fase 4H).

E1-E6 se ejecutan contra PostgreSQL real y usan las funciones de producción
``create_appointment``, ``cancel_appointment`` y ``reschedule_appointment``.

E1-E4 y E6 ejercitan además la brecha de parámetros tipados: el código de
producción pasa ``str`` contra columnas DATE/TIME/NUMERIC, que PostgreSQL
rechaza (a diferencia de SQLite). Esa brecha pertenece al milestone diferido
de adaptación tipada; por eso estos escenarios llevan también el marker
INFORMATIVO ``pg_typed`` — el marker no los excluye (el gating
``-m 'not pg_typed'`` fue eliminado), y en CI el resultado real queda a la
vista.

E3 congela la semántica ACTUAL que implementa services/appointments.py:
un turno con recurso bloquea ese recurso y los turnos globales
(resource_id IS NULL); un turno global bloquea todo el negocio. La decisión de
producto sobre la regla "recurso vs global" (F.5) sigue pendiente y NO se
inventa una nueva aquí.
"""

import datetime
import threading

import pytest

import services.appointments as appointments
from tests_pg._helpers import (
    connect_autocommit,
    make_pg_proxy,
    seed_business_settings,
    seed_confirmed_appointment,
    seed_full_week,
    seed_resource,
    seed_service,
)

PHONE = "5551234567"


def _run_concurrently(workers):
    """Ejecuta los workers reales en threads con salida simultánea (Barrier)."""
    barrier = threading.Barrier(len(workers))
    results = []

    def runner(worker):
        barrier.wait()
        try:
            results.append(worker())
        except Exception as exc:  # noqa: BLE001 - el error queda como resultado
            results.append(exc)

    threads = [threading.Thread(target=runner, args=(worker,)) for worker in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert all(not thread.is_alive() for thread in threads)
    return results


# ============================================================
# E5 - CANCELACIÓN CONCURRENTE (se ejecuta contra PG real)
# ============================================================


@pytest.mark.pg_live
def test_e5_cancelacion_concurrente(pg_pool, pg_seed, monkeypatch):
    """E5 F.5: dos cancelaciones simultáneas del mismo turno con token válido.

    Una operación gana (True), la otra devuelve False (el UPDATE condicional
    ``status='confirmed'`` ya no matchea), el estado final es ``cancelled`` y
    el pool queda utilizable sin transacciones abortadas.
    """
    token = "e5-management-token"
    appointment_id = seed_confirmed_appointment(
        pg_seed["url"], pg_seed["business_id"], token, "2026-10-01", "10:00", "10:30"
    )

    monkeypatch.setattr("services.appointments.get_connection", lambda: make_pg_proxy(pg_pool))

    def cancelar():
        return appointments.cancel_appointment(
            appointment_id, PHONE, business_id=pg_seed["business_id"], management_token=token
        )

    results = _run_concurrently([cancelar, cancelar])
    assert len(results) == 2
    assert all(isinstance(result, bool) for result in results), f"Errores en workers: {results}"
    assert sorted(results) == [False, True]

    with connect_autocommit(pg_seed["url"]) as conn:
        status_row = conn.execute(
            "SELECT status FROM appointments WHERE id = %s", (appointment_id,)
        ).fetchone()
    assert status_row[0] == "cancelled"

    probe = make_pg_proxy(pg_pool)
    try:
        row = probe.execute("SELECT 1 AS ok").fetchone()
        assert row[0] == 1
    finally:
        probe.close()


# ============================================================
# E1-E4 Y E6 - ESCENARIOS F.5 DE CREACIÓN/REPROGRAMACIÓN (pg_live)
# ============================================================

E5_DATE = "2026-10-05"
E6_TARGET_DATE = "2026-10-06"
NAME = "Cliente"


def _setup_booking_context(pg_seed):
    """Semilla mínima para los flujos de creación/reprogramación."""
    seed_business_settings(pg_seed["url"], pg_seed["business_id"])
    seed_full_week(pg_seed["url"], pg_seed["business_id"])
    seed_service(pg_seed["url"], pg_seed["business_id"], "Corte", 30)


@pytest.mark.pg_live
@pytest.mark.pg_typed
def test_e1_misma_franja_conflictiva(pg_pool, pg_seed, monkeypatch):
    """E1 F.5: misma franja sin recurso -> una gana y la otra queda ``occupied``."""
    _setup_booking_context(pg_seed)
    monkeypatch.setattr("services.appointments.get_connection", lambda: make_pg_proxy(pg_pool))

    def crear():
        return appointments.create_appointment(
            NAME, PHONE, "Corte", E5_DATE, "10:00", pg_seed["business_id"]
        )

    results = _run_concurrently([crear, crear])
    assert len(results) == 2
    assert all(isinstance(r, dict) and "success" in r for r in results), str(results)
    assert len([r for r in results if r["success"]]) == 1
    conflicts = [r for r in results if not r["success"]]
    assert len(conflicts) == 1
    assert conflicts[0]["reason"] == "occupied"


@pytest.mark.pg_live
@pytest.mark.pg_typed
def test_e2_misma_franja_distintos_recursos(pg_pool, pg_seed, monkeypatch):
    """E2 F.5: misma franja en recursos distintos -> ambos turnos se crean."""
    _setup_booking_context(pg_seed)
    resource_1 = seed_resource(pg_seed["url"], pg_seed["business_id"], "Silla 1")
    resource_2 = seed_resource(pg_seed["url"], pg_seed["business_id"], "Silla 2")
    monkeypatch.setattr("services.appointments.get_connection", lambda: make_pg_proxy(pg_pool))

    def crear(resource_id):
        return appointments.create_appointment(
            NAME, PHONE, "Corte", E5_DATE, "10:00", pg_seed["business_id"], resource_id=resource_id
        )

    results = _run_concurrently([lambda: crear(resource_1), lambda: crear(resource_2)])
    assert len(results) == 2
    assert all(isinstance(r, dict) and r["success"] for r in results), str(results)


@pytest.mark.pg_live
@pytest.mark.pg_typed
def test_e3_recurso_vs_global(pg_pool, pg_seed, monkeypatch):
    """E3 F.5: un turno con recurso y un turno global en la misma franja.

    Semántica CONGELADA de la implementación actual (appointments.py):
    el turno con recurso bloquea ese recurso y los globales (resource_id
    IS NULL); el turno global bloquea todo el negocio. Por lo tanto ambos
    intentos colisionan -> uno gana y el otro queda ``occupied``.
    """
    _setup_booking_context(pg_seed)
    resource = seed_resource(pg_seed["url"], pg_seed["business_id"], "Silla 1")
    monkeypatch.setattr("services.appointments.get_connection", lambda: make_pg_proxy(pg_pool))

    def crear_con_recurso():
        return appointments.create_appointment(
            NAME, PHONE, "Corte", E5_DATE, "10:00", pg_seed["business_id"], resource_id=resource
        )

    def crear_global():
        return appointments.create_appointment(
            NAME, PHONE, "Corte", E5_DATE, "10:00", pg_seed["business_id"]
        )

    results = _run_concurrently([crear_con_recurso, crear_global])
    assert len(results) == 2
    assert all(isinstance(r, dict) and "success" in r for r in results), str(results)
    assert len([r for r in results if r["success"]]) == 1
    conflicts = [r for r in results if not r["success"]]
    assert len(conflicts) == 1
    assert conflicts[0]["reason"] == "occupied"


@pytest.mark.pg_live
@pytest.mark.pg_typed
def test_e4_solapamiento_parcial(pg_pool, pg_seed, monkeypatch):
    """E4 F.5: intervalos parcialmente solapados (10:00-10:45 vs 10:30-11:15)."""
    seed_business_settings(pg_seed["url"], pg_seed["business_id"])
    seed_full_week(pg_seed["url"], pg_seed["business_id"])
    seed_service(pg_seed["url"], pg_seed["business_id"], "Corte", 45)
    monkeypatch.setattr("services.appointments.get_connection", lambda: make_pg_proxy(pg_pool))

    def crear(hora):
        return appointments.create_appointment(
            NAME, PHONE, "Corte", E5_DATE, hora, pg_seed["business_id"]
        )

    results = _run_concurrently([lambda: crear("10:00"), lambda: crear("10:30")])
    assert len(results) == 2
    assert all(isinstance(r, dict) and "success" in r for r in results), str(results)
    assert len([r for r in results if r["success"]]) == 1
    conflicts = [r for r in results if not r["success"]]
    assert len(conflicts) == 1
    assert conflicts[0]["reason"] == "occupied"


@pytest.mark.pg_live
@pytest.mark.pg_typed
def test_e6_reprogramacion_concurrente(pg_pool, pg_seed, monkeypatch):
    """E6 F.5: dos admin reprogramando el MISMO turno a la MISMA franja objetivo.

    El advisory lock del día objetivo serializa ambas reprogramaciones: la
    primera mueve el turno a la franja (``rescheduled``); la segunda vuelve a
    validar y ya no encuentra la franja disponible (get_available_slots no
    excluye el turno en movimiento, que quedó allí) -> ``invalid_time``.
    """
    _setup_booking_context(pg_seed)
    token = "e6-management-token"
    appointment_id = seed_confirmed_appointment(
        pg_seed["url"], pg_seed["business_id"], token, E5_DATE, "10:00", "10:30"
    )
    monkeypatch.setattr("services.appointments.get_connection", lambda: make_pg_proxy(pg_pool))

    def reprogramar():
        return appointments.reschedule_appointment(
            appointment_id,
            E6_TARGET_DATE,
            "11:00",
            PHONE,
            business_id=pg_seed["business_id"],
            management_token=token,
        )

    results = _run_concurrently([reprogramar, reprogramar])
    assert len(results) == 2
    assert all(isinstance(r, dict) and "success" in r for r in results), str(results)
    assert len([r for r in results if r["success"]]) == 1
    losers = [r for r in results if not r["success"]]
    assert len(losers) == 1
    assert losers[0]["reason"] == "invalid_time"

    with connect_autocommit(pg_seed["url"]) as conn:
        row = conn.execute(
            "SELECT appointment_date, appointment_time FROM appointments WHERE id = %s",
            (appointment_id,),
        ).fetchone()
    assert row[0] == datetime.date.fromisoformat(E6_TARGET_DATE)
    assert row[1] == datetime.time(11, 0)
