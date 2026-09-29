"""Pruebas REALES de la serialización de reprogramación y cancelación admin.

Una reprogramación no es una escritura de un solo día: LIBERA la franja del día
de origen y OCUPA la del destino. Por eso necesita el advisory lock de LOS DOS
días, y en ORDEN ASCENDENTE para que dos reprogramaciones concurrentes no puedan
deadlockearse entre sí.

Estos tests demuestran ese comportamiento contra PostgreSQL real, sin mocks: lo
único que se enruta al pool es la fuente de conexión (igual que en el resto de
la suite pg). Cada uno tiene un gemelo en SQLite
(``tests/test_appointments.py``) que fija el CONTRATO; aquí se fija la
SERIALIZACIÓN de verdad.
"""

import datetime
import threading
import time

import pytest

import database.database as database
import services.appointments as appointments
from database.database import acquire_business_write_lock
from tests_pg._helpers import (
    connect_autocommit,
    make_pg_proxy,
    seed_business_settings,
    seed_confirmed_appointment,
    seed_full_week,
    seed_service,
)

PHONE = "5551234567"

ORIGEN = "2026-11-02"  # lunes
DESTINO = "2026-11-03"  # martes
DIA_A = "2026-11-09"  # lunes
DIA_B = "2026-11-11"  # miércoles


def _setup_booking_context(pg_seed):
    """Semilla mínima para los flujos de creación/reprogramación."""
    seed_business_settings(pg_seed["url"], pg_seed["business_id"])
    seed_full_week(pg_seed["url"], pg_seed["business_id"])
    seed_service(pg_seed["url"], pg_seed["business_id"], "Corte", 30)


def _read_appointment(url, appointment_id):
    with connect_autocommit(url) as conn:
        return conn.execute(
            "SELECT status, appointment_date, appointment_time FROM appointments WHERE id = %s",
            (appointment_id,),
        ).fetchone()


def _spawn(worker, result, name="worker"):
    """Arranca `worker` en un thread. Su valor (o excepción) queda en result['value'].

    Mientras el thread está bloqueado, `value` NO existe: eso es lo que permite
    comprobar que la operación respeta el lock ajeno en vez de terminar.
    """

    def runner():
        try:
            result["value"] = worker()
        except Exception as exc:  # noqa: BLE001 - el error queda como resultado
            result["value"] = exc

    thread = threading.Thread(target=runner, name=name)
    thread.start()
    return thread


def _hold_day_lock(pg_pool, business_id, appointment_date, held, release):
    """Sostiene el advisory lock del día indicado hasta que se libere `release`."""
    proxy = make_pg_proxy(pg_pool)
    try:
        proxy.execute("BEGIN IMMEDIATE")
        acquire_business_write_lock(
            proxy, business_id, appointments._appointment_day_ordinal(appointment_date)
        )
        held.set()
        release.wait(timeout=15)
        proxy.commit()
    finally:
        proxy.close()


def _blocker(pg_pool, business_id, appointment_date):
    """Devuelve (thread, held, release): un holder del lock del día."""
    held = threading.Event()
    release = threading.Event()
    thread = threading.Thread(
        target=_hold_day_lock,
        args=(pg_pool, business_id, appointment_date, held, release),
        name=f"holder-{appointment_date}",
    )
    thread.start()
    assert held.wait(timeout=15), "el holder no tomó el advisory lock"
    return thread, held, release


def _finish(thread, release, result):
    """Libera el holder y espera a que el worker bloqueado termine."""
    release.set()
    thread.join(timeout=15)
    assert not thread.is_alive()
    return result["value"]


@pytest.mark.pg_live
@pytest.mark.pg_typed
def test_reprogramacion_respeta_el_lock_del_dia_de_origen(pg_pool, pg_seed, monkeypatch):
    """La reprogramación se BLOQUEA si otra transacción sostiene el DÍA DE ORIGEN.

    Es la regresión central: sin ese lock, la reprogramación se commitearía
    durante el hold y este test fallaría en la primera aserción.

    No es un detalle interno. Mientras el turno sigue en el día de origen, una
    reserva concurrente sobre esa franja tiene que ver el slot ocupado (para no
    venderlo dos veces) y, una vez liberado, tiene que poder ocuparlo. Eso exige
    que reprogramar y reservar se excluyan sobre el mismo día.
    """
    _setup_booking_context(pg_seed)
    business_id = pg_seed["business_id"]
    token = "origen-management-token"
    appointment_id = seed_confirmed_appointment(
        pg_seed["url"], business_id, token, ORIGEN, "10:00", "10:30"
    )
    monkeypatch.setattr("services.appointments.get_connection", lambda: make_pg_proxy(pg_pool))

    holder, _held, release = _blocker(pg_pool, business_id, ORIGEN)
    resultado = {}
    try:
        reprogramar = _spawn(
            lambda: appointments.reschedule_appointment(
                appointment_id,
                DESTINO,
                "11:00",
                PHONE,
                business_id=business_id,
                management_token=token,
            ),
            resultado,
            name="reprogramar",
        )
        time.sleep(0.5)
        assert "value" not in resultado, (
            "reschedule_appointment no respetó el advisory lock del día de origen"
        )
        fila = _read_appointment(pg_seed["url"], appointment_id)
        assert fila[0] == "confirmed", "el turno cambió sin tomar el lock del día de origen"
        assert fila[1] == datetime.date.fromisoformat(ORIGEN)
    finally:
        valor = _finish(reprogramar, release, resultado)
        holder.join(timeout=15)

    assert isinstance(valor, dict), valor
    assert valor["success"] is True, valor

    fila = _read_appointment(pg_seed["url"], appointment_id)
    assert fila[0] == "confirmed"
    assert fila[1] == datetime.date.fromisoformat(DESTINO)
    assert fila[2] == datetime.time(11, 0)


@pytest.mark.pg_live
@pytest.mark.pg_typed
def test_reprogramacion_admin_respeta_el_lock_del_dia_de_origen(pg_pool, pg_seed, monkeypatch):
    """La variante admin toma el MISMO lock de origen que la del cliente.

    Si el panel reprogramara sin ese lock, la serialización dependería de qué
    botón tocó el usuario: el cliente quedaría protegido y el panel no.
    """
    _setup_booking_context(pg_seed)
    business_id = pg_seed["business_id"]
    appointment_id = seed_confirmed_appointment(
        pg_seed["url"], business_id, "admin-token", ORIGEN, "10:00", "10:30"
    )
    monkeypatch.setattr("services.appointments.get_connection", lambda: make_pg_proxy(pg_pool))

    holder, _held, release = _blocker(pg_pool, business_id, ORIGEN)
    resultado = {}
    try:
        reprogramar = _spawn(
            lambda: appointments.reschedule_appointment_admin(
                appointment_id, DESTINO, "11:00", business_id=business_id
            ),
            resultado,
            name="reprogramar-admin",
        )
        time.sleep(0.5)
        assert "value" not in resultado, (
            "reschedule_appointment_admin no respetó el advisory lock del día de origen"
        )
    finally:
        valor = _finish(reprogramar, release, resultado)
        holder.join(timeout=15)

    assert isinstance(valor, dict), valor
    assert valor["success"] is True, valor
    fila = _read_appointment(pg_seed["url"], appointment_id)
    assert fila[1] == datetime.date.fromisoformat(DESTINO)
    assert fila[2] == datetime.time(11, 0)


@pytest.mark.pg_live
@pytest.mark.pg_typed
def test_orden_ascendente_impide_deadlock_entre_reprogramaciones(pg_pool, pg_seed, monkeypatch):
    """Dos turnos que se cambian de día ENCRUZADOS no se bloquean mutuamente.

    ``A`` va de ``DIA_A`` a ``DIA_B`` y ``B`` de ``DIA_B`` a ``DIA_A``. Si cada
    uno tomara origen y destino en el orden en que los necesita, A esperaría el
    lock de ``DIA_B`` mientras B esperaría el de ``DIA_A``, y PostgreSQL mataría
    una de las dos transacciones con un error de deadlock. Como los locks se
    toman SIEMPRE en orden ascendente, las dos empiezan por el mismo día (el
    menor) y ambas operaciones terminan bien.
    """
    _setup_booking_context(pg_seed)
    business_id = pg_seed["business_id"]
    token_a = "cruce-token-a"
    token_b = "cruce-token-b"
    turno_a = seed_confirmed_appointment(
        pg_seed["url"], business_id, token_a, DIA_A, "10:00", "10:30"
    )
    turno_b = seed_confirmed_appointment(
        pg_seed["url"], business_id, token_b, DIA_B, "10:00", "10:30"
    )
    monkeypatch.setattr("services.appointments.get_connection", lambda: make_pg_proxy(pg_pool))

    barrera = threading.Barrier(2)
    resultado_a = {}
    resultado_b = {}

    def mover(appointment_id, token, destino_iso):
        barrera.wait(timeout=15)
        return appointments.reschedule_appointment(
            appointment_id,
            destino_iso,
            "11:00",
            PHONE,
            business_id=business_id,
            management_token=token,
        )

    hilos = [
        _spawn(lambda: mover(turno_a, token_a, DIA_B), resultado_a, name="mover-a"),
        _spawn(lambda: mover(turno_b, token_b, DIA_A), resultado_b, name="mover-b"),
    ]
    for hilo in hilos:
        hilo.join(timeout=30)

    assert all(not hilo.is_alive() for hilo in hilos), "deadlock entre reprogramaciones cruzadas"
    for nombre, valor in (("a", resultado_a["value"]), ("b", resultado_b["value"])):
        assert isinstance(valor, dict), f"{nombre} no devolvió un dict: {valor!r}"
        assert "deadlock" not in str(valor).lower(), valor
        assert valor["success"] is True, f"{nombre} falló: {valor}"

    assert _read_appointment(pg_seed["url"], turno_a)[1] == datetime.date.fromisoformat(DIA_B)
    assert _read_appointment(pg_seed["url"], turno_b)[1] == datetime.date.fromisoformat(DIA_A)


@pytest.mark.pg_live
@pytest.mark.pg_typed
def test_reprogramar_y_cancelar_el_mismo_turno_no_inventa_exito(pg_pool, pg_seed, monkeypatch):
    """Reprogramar y cancelar a la vez: nunca un éxito que no ocurrió.

    Ambas operaciones toman el lock del día donde el turno está. Gane quien
    gane, la invariante es la misma: si la reprogramación devuelve ``success``,
    la fila tiene que estar realmente en la fecha destino y ``confirmed``. Con
    el ``rowcount`` sin verificar, la reprogramación perdedora devolvía
    ``rescheduled`` sobre un turno ya cancelado.
    """
    _setup_booking_context(pg_seed)
    business_id = pg_seed["business_id"]
    token = "carrera-token"
    appointment_id = seed_confirmed_appointment(
        pg_seed["url"], business_id, token, ORIGEN, "10:00", "10:30"
    )
    monkeypatch.setattr("services.appointments.get_connection", lambda: make_pg_proxy(pg_pool))

    barrera = threading.Barrier(2)
    resultado_reprogramar = {}
    resultado_cancelar = {}

    def reprogramar():
        barrera.wait(timeout=15)
        return appointments.reschedule_appointment(
            appointment_id, DESTINO, "11:00", PHONE, business_id=business_id, management_token=token
        )

    def cancelar():
        barrera.wait(timeout=15)
        return appointments.cancel_appointment(
            appointment_id, PHONE, business_id=business_id, management_token=token
        )

    hilos = [
        _spawn(reprogramar, resultado_reprogramar, name="reprogramar"),
        _spawn(cancelar, resultado_cancelar, name="cancelar"),
    ]
    for hilo in hilos:
        hilo.join(timeout=30)
    assert all(not hilo.is_alive() for hilo in hilos), "deadlock entre reprogramar y cancelar"

    reprogramado = resultado_reprogramar["value"]
    cancelado = resultado_cancelar["value"]
    assert isinstance(reprogramado, dict), reprogramado
    assert isinstance(cancelado, bool), cancelado

    fila = _read_appointment(pg_seed["url"], appointment_id)
    if reprogramado["success"]:
        # Gana la reprogramación: el turno está donde dice, confirmado.
        assert fila[0] == "confirmed", fila
        assert fila[1] == datetime.date.fromisoformat(DESTINO), fila
        assert fila[2] == datetime.time(11, 0), fila
    else:
        # Pierde: el turno no puede haberse movido, y la razón es explícita.
        assert reprogramado["reason"] == "not_found", reprogramado
        assert cancelado is True
        assert fila[0] == "cancelled", fila
        assert fila[1] == datetime.date.fromisoformat(ORIGEN), fila
        assert fila[2] == datetime.time(10, 0), fila

    probe = make_pg_proxy(pg_pool)
    try:
        assert probe.execute("SELECT 1 AS ok").fetchone()[0] == 1
    finally:
        probe.close()


@pytest.mark.pg_live
@pytest.mark.pg_typed
def test_lock_timeout_agotado_devuelve_busy_sin_mover_el_turno(pg_pool, pg_seed, monkeypatch):
    """Si otro turno sostiene el día, la reprogramación cede en vez de caer en 500.

    El ``lock_timeout`` de la sesión corta la espera; el servicio lo traduce a un
    motivo reintentable y hace ROLLBACK, así que el turno queda intacto y el pool
    sigue utilizable (sin transacción abortada colgando).
    """

    def proxy_con_timeout_corto():
        proxy = make_pg_proxy(pg_pool)
        proxy.execute("SET lock_timeout = '300ms'")
        proxy.commit()
        return proxy

    _setup_booking_context(pg_seed)
    business_id = pg_seed["business_id"]
    token = "timeout-token"
    appointment_id = seed_confirmed_appointment(
        pg_seed["url"], business_id, token, ORIGEN, "10:00", "10:30"
    )
    monkeypatch.setattr("services.appointments.get_connection", lambda: proxy_con_timeout_corto())

    holder, _held, release = _blocker(pg_pool, business_id, ORIGEN)
    try:
        resultado = appointments.reschedule_appointment(
            appointment_id, DESTINO, "11:00", PHONE, business_id=business_id, management_token=token
        )
    finally:
        release.set()
        holder.join(timeout=15)

    assert resultado["success"] is False
    assert resultado["reason"] == "busy"
    fila = _read_appointment(pg_seed["url"], appointment_id)
    assert fila[0] == "confirmed"
    assert fila[1] == datetime.date.fromisoformat(ORIGEN)
    assert fila[2] == datetime.time(10, 0)

    monkeypatch.undo()
    probe = make_pg_proxy(pg_pool)
    try:
        assert probe.execute("SELECT 1 AS ok").fetchone()[0] == 1
    finally:
        probe.close()


@pytest.mark.pg_live
@pytest.mark.pg_typed
def test_cancelacion_admin_respeta_el_lock_del_dia_del_turno(pg_pool, pg_seed, monkeypatch):
    """`update_appointment_status_scoped` entra en la serialización del calendario.

    Antes hacía un UPDATE suelto, sin transacción ni lock: la cancelación del
    panel podía colarse durante el hold de otra operación. Ahora toma el mismo
    lock `(business_id, día)` que crear, cancelar y reprogramar.
    """
    _setup_booking_context(pg_seed)
    business_id = pg_seed["business_id"]
    appointment_id = seed_confirmed_appointment(
        pg_seed["url"], business_id, "admin-cancel-token", ORIGEN, "10:00", "10:30"
    )
    monkeypatch.setattr("database.database.get_connection", lambda: make_pg_proxy(pg_pool))

    holder, _held, release = _blocker(pg_pool, business_id, ORIGEN)
    resultado = {}
    try:
        cancelar = _spawn(
            lambda: database.update_appointment_status_scoped(
                appointment_id, "cancelled", business_id
            ),
            resultado,
            name="cancelar-admin",
        )
        time.sleep(0.5)
        assert "value" not in resultado, (
            "update_appointment_status_scoped no respetó el advisory lock del día"
        )
        assert _read_appointment(pg_seed["url"], appointment_id)[0] == "confirmed"
    finally:
        valor = _finish(cancelar, release, resultado)
        holder.join(timeout=15)

    assert valor is True, valor
    assert _read_appointment(pg_seed["url"], appointment_id)[0] == "cancelled"


@pytest.mark.pg_live
@pytest.mark.pg_typed
def test_cancelacion_admin_no_resucita_turno_cancelado(pg_pool, pg_seed, monkeypatch):
    """El panel no puede devolver a `confirmed` un turno que ya está cancelado.

    Sin el predicado de estado, un operador podía "reactivar" desde el panel un
    turno que el cliente había cancelado desde su enlace.
    """
    _setup_booking_context(pg_seed)
    business_id = pg_seed["business_id"]
    appointment_id = seed_confirmed_appointment(
        pg_seed["url"], business_id, "resurreccion-token", ORIGEN, "10:00", "10:30"
    )
    monkeypatch.setattr("database.database.get_connection", lambda: make_pg_proxy(pg_pool))

    assert (
        database.update_appointment_status_scoped(appointment_id, "cancelled", business_id) is True
    )
    assert (
        database.update_appointment_status_scoped(appointment_id, "confirmed", business_id) is False
    )
    assert _read_appointment(pg_seed["url"], appointment_id)[0] == "cancelled"

    # Corregir desde `completed` sigue siendo una operación legítima del operador.
    otro = seed_confirmed_appointment(
        pg_seed["url"], business_id, "correccion-token", DESTINO, "10:00", "10:30"
    )
    assert database.update_appointment_status_scoped(otro, "completed", business_id) is True
    assert database.update_appointment_status_scoped(otro, "confirmed", business_id) is True
    assert _read_appointment(pg_seed["url"], otro)[0] == "confirmed"
