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

from database.database import acquire_business_write_lock
from tests_pg._helpers import connect_autocommit, make_pg_proxy


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
