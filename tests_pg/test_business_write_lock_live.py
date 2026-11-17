"""Concurrencia REAL del advisory lock de escritura por negocio (Pruebas A-F).

Cada prueba usa conexiones PostgreSQL reales (pool + conexiones autocommit de
sondeo) contra ``acquire_business_write_lock`` de producción. No hay mocks ni
``sleep`` como única evidencia: la serialización se demuestra con eventos
acotados por timeout y con el sondeo estructural
``pg_try_advisory_xact_lock(%s::bigint)`` usando la MISMA clave combinada que
producción (``business_lock_key``).

Cubre:
  A. Mismo (business_id, lock_key) -> la segunda conexión espera.
  B. Distinto business_id, misma lock_key -> no se bloquea.
  C. Mismo business_id, distinta lock_key -> no se bloquea.
  D. COMMIT libera el lock.
  E. ROLLBACK libera el lock.
  F. lock_key = 3_000_000_001 (el valor que rompía la firma int4) funciona y
     sigue aislando negocios.
"""

import threading

import pytest

from database.database import acquire_business_write_lock, business_lock_key
from tests_pg._helpers import connect_autocommit, make_pg_proxy

BIZ_A = 101
BIZ_B = 202
KEY_X = 987654
KEY_Y = 555
KEY_BIG = 3_000_000_001

HOLD_TIMEOUT = 15
BLOCK_WINDOW = 0.6


def _probe_try(url, business_id, lock_key):
    """¿Está libre ahora la clave combinada? (xact lock en autocommit)."""
    combined = business_lock_key(business_id, lock_key)
    with connect_autocommit(url) as conn:
        row = conn.execute(
            "SELECT pg_try_advisory_xact_lock(%s::bigint)", (combined,)
        ).fetchone()
    return bool(row[0])


class _Holder:
    """Sostiene el advisory lock en una conexión del pool hasta liberarlo."""

    def __init__(self, pool, business_id, lock_key, finish="commit"):
        self.proxy = make_pg_proxy(pool)
        self.business_id = business_id
        self.lock_key = lock_key
        self.finish = finish
        self.acquired = threading.Event()
        self.release = threading.Event()
        self.error = None
        self._thread = threading.Thread(target=self._run)

    def _run(self):
        try:
            self.proxy.execute("BEGIN IMMEDIATE")
            acquire_business_write_lock(self.proxy, self.business_id, self.lock_key)
            self.acquired.set()
            self.release.wait(timeout=HOLD_TIMEOUT)
            if self.finish == "commit":
                self.proxy.commit()
            else:
                self.proxy.rollback()
        except BaseException as exc:  # pragma: no cover - se reporta abajo
            self.error = exc
        finally:
            self.proxy.close()

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self.release.set()
        self._thread.join(timeout=HOLD_TIMEOUT)
        assert not self._thread.is_alive(), "el holder no terminó"
        assert self.error is None, f"holder falló: {self.error!r}"


@pytest.mark.pg_live
def test_a_misma_clave_serializa(pg_pool):
    """A: mismo (business_id, lock_key) -> B espera hasta el COMMIT de A."""
    a = _Holder(pg_pool, BIZ_A, KEY_X).start()
    try:
        assert a.acquired.wait(HOLD_TIMEOUT), "A no adquirió el lock"

        b = _Holder(pg_pool, BIZ_A, KEY_X).start()
        try:
            assert not b.acquired.wait(BLOCK_WINDOW), (
                "B adquirió el lock mientras A lo sostenía (no serializa)"
            )
            a.stop()
            assert b.acquired.wait(HOLD_TIMEOUT), "B no retomó tras liberar A"
        finally:
            b.stop()
    finally:
        a.stop()


@pytest.mark.pg_live
def test_b_distinto_negocio_no_bloquea(pg_pool):
    """B: distinto business_id con la misma lock_key no se bloquea."""
    a = _Holder(pg_pool, BIZ_A, KEY_X).start()
    try:
        assert a.acquired.wait(HOLD_TIMEOUT), "A no adquirió el lock"
        b = _Holder(pg_pool, BIZ_B, KEY_X).start()
        try:
            assert b.acquired.wait(HOLD_TIMEOUT), "B quedó bloqueado por otro negocio"
        finally:
            b.stop()
    finally:
        a.stop()


@pytest.mark.pg_live
def test_c_distinta_clave_no_bloquea(pg_pool):
    """C: mismo business_id con distinta lock_key no se bloquea."""
    a = _Holder(pg_pool, BIZ_A, KEY_X).start()
    try:
        assert a.acquired.wait(HOLD_TIMEOUT), "A no adquirió el lock"
        b = _Holder(pg_pool, BIZ_A, KEY_Y).start()
        try:
            assert b.acquired.wait(HOLD_TIMEOUT), "B quedó bloqueado por otra clave"
        finally:
            b.stop()
    finally:
        a.stop()


@pytest.mark.pg_live
def test_d_commit_libera(pg_pool, pg_test_database):
    """D: COMMIT libera el xact lock."""
    holder = _Holder(pg_pool, BIZ_A, KEY_X, finish="commit").start()
    try:
        assert holder.acquired.wait(HOLD_TIMEOUT)
        assert _probe_try(pg_test_database, BIZ_A, KEY_X) is False
    finally:
        holder.stop()
    assert _probe_try(pg_test_database, BIZ_A, KEY_X) is True


@pytest.mark.pg_live
def test_e_rollback_libera(pg_pool, pg_test_database):
    """E: ROLLBACK libera el xact lock."""
    holder = _Holder(pg_pool, BIZ_A, KEY_X, finish="rollback").start()
    try:
        assert holder.acquired.wait(HOLD_TIMEOUT)
        assert _probe_try(pg_test_database, BIZ_A, KEY_X) is False
    finally:
        holder.stop()
    assert _probe_try(pg_test_database, BIZ_A, KEY_X) is True


@pytest.mark.pg_live
def test_f_lock_key_mayor_a_int4_funciona_y_aisla(pg_pool, pg_test_database):
    """F: lock_key = 3_000_000_001 adquiere, serializa y aísla negocios."""
    # El valor que rompía la firma int4: el holder debe adquirirlo SIN error.
    holder = _Holder(pg_pool, BIZ_A, KEY_BIG, finish="commit").start()
    try:
        assert holder.acquired.wait(HOLD_TIMEOUT), (
            "no se pudo adquirir el lock con lock_key=3_000_000_001"
        )
        assert _probe_try(pg_test_database, BIZ_A, KEY_BIG) is False
        # Aislamiento: otro negocio con la MISMA clave grande sigue libre.
        assert _probe_try(pg_test_database, BIZ_B, KEY_BIG) is True
    finally:
        holder.stop()
    assert _probe_try(pg_test_database, BIZ_A, KEY_BIG) is True
