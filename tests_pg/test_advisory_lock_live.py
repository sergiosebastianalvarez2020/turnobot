"""Prueba REAL del advisory lock de negocio (Fase 4H).

Dos conexiones PostgreSQL reales compiten por ``pg_advisory_xact_lock`` sobre
el mismo ``(business_id, lock_key)``. Se demuestra serialización REAL: la
segunda conexión NO puede avanzar mientras la primera mantiene abierta su
transacción; recién al COMMIT de la primera (que libera el xact lock) la
segunda puede continuar. Sin mocks: se usa ``acquire_business_write_lock`` de
producción contra las conexiones del pool.

Además se verifica de forma estructural (sin depender del timing) que PG
realmente mantiene el lock mientras lo sostiene A: una conexión probe que
intenta ``pg_try_advisory_xact_lock`` sobre el mismo par debe responder
False durante el hold de A y True una vez liberado.
"""

import threading
import time

import pytest

import services.appointments as appointments
from database.database import acquire_business_write_lock
from tests_pg._helpers import connect_autocommit, make_pg_proxy, seed_confirmed_appointment

CANCEL_DATE = "2026-10-01"
CANCEL_TOKEN = "cancel-lock-management-token"
PHONE = "5551234567"


@pytest.mark.pg_live
def test_advisory_lock_serializa_conexiones_reales(pg_pool, pg_test_database):
    proxy_a = make_pg_proxy(pg_pool)
    proxy_b = make_pg_proxy(pg_pool)
    business_id = 42
    lock_key = 987654

    a_locked = threading.Event()
    b_started = threading.Event()
    release_a = threading.Event()
    record = {"a_released": None, "b_started": None, "b_done": None}

    def worker_a():
        try:
            proxy_a.execute("BEGIN IMMEDIATE")
            acquire_business_write_lock(proxy_a, business_id, lock_key)
            a_locked.set()
            release_a.wait(timeout=15)
            record["a_released"] = time.monotonic()
            proxy_a.commit()
        finally:
            proxy_a.close()

    def worker_b():
        try:
            proxy_b.execute("BEGIN IMMEDIATE")
            record["b_started"] = time.monotonic()
            b_started.set()
            # Se BLOQUEA aquí hasta que A libere el xact lock.
            acquire_business_write_lock(proxy_b, business_id, lock_key)
            record["b_done"] = time.monotonic()
            proxy_b.commit()
        finally:
            proxy_b.close()

    def try_lock() -> bool:
        with connect_autocommit(pg_test_database) as probe:
            row = probe.execute(
                "SELECT pg_try_advisory_xact_lock(%s, %s)", (business_id, lock_key)
            ).fetchone()
        return bool(row[0])

    thread_a = threading.Thread(target=worker_a)
    thread_b = threading.Thread(target=worker_b)
    thread_a.start()
    assert a_locked.wait(timeout=15)
    # Mientras A mantiene el lock, un tercer intento NO puede adquirirlo.
    assert try_lock() is False
    thread_b.start()
    assert b_started.wait(timeout=15)
    time.sleep(0.4)
    assert record["b_done"] is None
    release_a.set()
    thread_a.join(timeout=15)
    thread_b.join(timeout=15)
    assert not thread_a.is_alive()
    assert not thread_b.is_alive()
    assert record["b_started"] is not None
    assert record["b_done"] is not None
    assert record["b_started"] < record["a_released"]
    assert record["b_done"] >= record["a_released"]
    # Liberado el lock, el mismo intento ya adquiere.
    assert try_lock() is True


@pytest.mark.pg_live
def test_cancel_appointment_respeta_el_advisory_lock(pg_pool, pg_seed, monkeypatch):
    """``cancel_appointment`` de producción se BLOQUEA tras el lock ajeno.

    Demuestra que la cancelación toma el advisory lock ``(business_id, día)``
    con la misma clave que usa ``create_appointment``: mientras otra conexión
    lo sostiene, el ``UPDATE ... status='cancelled'`` NO se ejecuta. Al liberar,
    la cancelación retoma y completa.

    Sin el lock, la cancelación se commitearía durante el hold y este test
    fallaría en el primer ``assert``. No usa mocks: solo la fuente de conexión
    se enruta al pool (igual que en el resto de la suite pg).
    """
    appointment_id = seed_confirmed_appointment(
        pg_seed["url"], pg_seed["business_id"], CANCEL_TOKEN, CANCEL_DATE, "10:00", "10:30"
    )
    business_id = pg_seed["business_id"]
    lock_key = appointments._appointment_day_ordinal(CANCEL_DATE)

    monkeypatch.setattr("services.appointments.get_connection", lambda: make_pg_proxy(pg_pool))

    holder = make_pg_proxy(pg_pool)
    held = threading.Event()
    release = threading.Event()
    record = {"cancel_done": None, "status_while_held": None}

    def hold_lock():
        try:
            holder.execute("BEGIN IMMEDIATE")
            acquire_business_write_lock(holder, business_id, lock_key)
            held.set()
            release.wait(timeout=15)
            holder.commit()
        finally:
            holder.close()

    def status_now():
        with connect_autocommit(pg_seed["url"]) as probe:
            return probe.execute(
                "SELECT status FROM appointments WHERE id = %s", (appointment_id,)
            ).fetchone()[0]

    def cancelar():
        record["cancel_done"] = appointments.cancel_appointment(
            appointment_id, PHONE, business_id=business_id, management_token=CANCEL_TOKEN
        )

    holder_thread = threading.Thread(target=hold_lock)
    cancel_thread = threading.Thread(target=cancelar)
    holder_thread.start()
    try:
        assert held.wait(timeout=15)
        cancel_thread.start()
        time.sleep(0.4)
        assert record["cancel_done"] is None, "cancel_appointment no respetó el advisory lock"
        record["status_while_held"] = status_now()
        assert record["status_while_held"] == "confirmed", "el turno se canceló sin tomar el lock"
    finally:
        release.set()
        holder_thread.join(timeout=15)
        cancel_thread.join(timeout=15)

    assert not holder_thread.is_alive()
    assert not cancel_thread.is_alive()
    assert record["cancel_done"] is True
    assert status_now() == "cancelled"
