"""Contratos de seguridad P2-01: verificación de management_token en turnos.

Corrección cubierta (P2-01): GET /b/<slug>/api/turnos?nombre=&telefono=
no expone PII sin que el llamador posea el management_token emitido al
reservar.

Implementaciones aplicadas:
* services/appointments.py: get_customer_appointments() valida la posesión del
  token (sha256(management_token) en la BD + nombre/teléfono + business_id).
  Sobre cualquier fallo devuelve None: sin token, inválido, de otro cliente o
  de otro negocio -> mismo 401 sin exfiltración de PII.
* routes/public_api.py: _get_public_appointments_response() exige el token en la
  query string y responde siempre 401 con {"code": "verification_required",
  "error": <mensaje idéntico>}, sin nunca exponer ningún dato de la BD.
* services/ai.py: la herramienta buscar_turnos_cliente y su ejecutor
  _execute_buscar_turnos_cliente requieren management_token; sin el, la IA no
  devuelve turnos del cliente.

 Esta suite es autocontenida en SQLite (no requiere TURNOBOT_PG_URL) y
 cubre:

 1. HTTP: 401 idéntica y sin PII: sin token, token inválido, de otro cliente,
    de otro negocio, malformada, de longitud > 256.
 2. HTTP: token válido -> 200 con sólo turnos del cliente verificado del
    mismo business, respetando business_id resuelto por el slug.
 3. AI: _execute_buscar_turnos_cliente sin token -> sin turnos; con token válido
    -> sólo turnos verificados de su business.
 4. Rate limit api:turnos (60/min/ip+business) aplicado ANTES de la
    verificación (antes de que el token se evalúe en el servicio).
"""

import hashlib

import pytest

import app as application

# Aislamiento de estado SQLite por test (P2-01): get_connection(),
# init_database() y apply_migrations() operan sobre DATABASE_PATH, la
# variable global del módulo database/database.py. Las variables de entorno
# NO modifican el módulo ya importado, por eso cada test redirige ese global a
# un archivo SQLite temporal propio mediante la fixture `turnos_env`
# (tmp_path + monkeypatch), que además siembra los datos y garantiza que TANTO
# las pruebas HTTP COMO las de IA compartan exactamente la misma base aislada.
import database.database as database
from application import create_app
from services.appointments import normalize_phone

TURNOS_URL = "/b/biz-p2/api/turnos?nombre=Cliente%20P2&telefono=11-5555-1234"

_TURNOS_ERROR = (
    "No pudimos verificar tu consulta. Ingresá el management_token "
    "que recibiste al reservar (en el enlace de gestión del turno)."
)


@pytest.fixture(autouse=True)
def pg_test_env():
    """Esta suite es SQLite autocontenida.

    Anula el skip funcional de tests/conftest.py, que omite la suite funcional
    PostgreSQL cuando TURNOBOT_PG_URL no está definida. El aislamiento SQLite
    real lo aplica `turnos_env` (DATABASE_PATH -> tmp_path por test); aquí no se
    requiere PostgreSQL.
    """
    yield


@pytest.fixture(autouse=True)
def pg_isolate_module_app():
    """Opt-out del reconfigurado PostgreSQL del conftest.

    La suite es SQLite: no se crea ni se reconfigura ninguna base PostgreSQL
    para el app de módulo. `turnos_env` construye una app SQLite propia con
    DATABASE_PATH ya redirigido al archivo temporal del test.
    """
    yield


@pytest.fixture()
def turnos_env(tmp_path, monkeypatch):
    """App Flask + base SQLite aislada y sembrada para P2-01.

    Aislamiento controlado (P2-01): cada test escribe e inicializa el esquema
    en un archivo SQLite propio, apuntando `database.database.DATABASE_PATH`
    sobre la variable global que usan `get_connection()` e `init_database()`
    (las variables de entorno no alcanzan porque el módulo ya fue importado).
    El monkeypatch restablece el estado al terminar el test.

    `create_app()` ya ejecuta `init_database()` -> `apply_migrations()` sobre
    `DATABASE_PATH`, por lo que NO se vuelve a inicializar el esquema aquí (evita
    la doble inicialización). El teardown cierra la conexión de siembra antes de
    eliminar el archivo temporal (SQLite en WAL crea además -wal/-shm).
    """
    db_file = tmp_path / "turnos_test.db"

    monkeypatch.setattr(database, "DATABASE_PATH", db_file)
    # Entorno coherente con el backend SQLite seleccionado.
    monkeypatch.setenv("DATABASE_URL", "sqlite:///" + db_file.as_posix())
    monkeypatch.setenv("DB_BACKEND", "sqlite")
    monkeypatch.delenv("TURNOBOT_PG_URL", raising=False)

    app = create_app()

    conn = database.get_connection()
    try:
        seeded_ids = _seed(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    # Las migraciones ya crean el business 1 ('el-corte'); los businesses de
    # esta suite reciben ids autoincrementales, así que se exponen para que los
    # tests no los asuman (p. ej. biz-p2 NO es necesariamente el id 1).
    app.config["P2_SEED_IDS"] = seeded_ids

    # Limpiar contadores de rate limit antes de cada test (IP 127.0.0.1 y
    # business biz-p2): el estado es compartido por el dispatcher de rutas.
    application.rate_limit_state.clear()

    try:
        yield app
    finally:
        # No quedan conexiones abiertas por la fixture (se cerró la de siembra);
        # se eliminan el archivo y sus auxiliares WAL/SHM.
        for suffix in ("", "-wal", "-shm"):
            candidate = db_file.with_name(db_file.name + suffix)
            if candidate.exists():
                candidate.unlink()


@pytest.fixture()
def turnos_client(turnos_env):
    """Cliente HTTP sobre la app SQLite aislada de `turnos_env`."""
    return turnos_env.test_client()


def _seed(conn):
    """Siembra dos negocios con sus turnos y devuelve los ids reales.

    Las migraciones ya insertan el business 1 ('el-corte'), por lo que los
    negocios de esta suite obtienen ids autoincrementales; se captura el
    `lastrowid` de cada uno en lugar de asumir 1 y 2.
    """
    biz_p2_id = conn.execute(
        "INSERT INTO businesses (name, slug, active, pending) VALUES (?, ?, 1, 1)",
        ("Negocio P2-01", "biz-p2"),
    ).lastrowid
    conn.execute(
        "INSERT INTO services (business_id, name, price, duration, active) "
        "VALUES (?, 'Servicio P2', 1000, 30, 1)",
        (biz_p2_id,),
    )

    # Turno del cliente verificado (token 1) en biz-p2. Hora propia para no
    # colisionar con el índice UNIQUE parcial de slot confirmado.
    _seed_appointment(conn, "Cliente P2", "11-5555-1234", "tok-p2-0001", biz_p2_id, "10:00")

    # Turno de otro cliente, mismo business (para rechazo de propiedad).
    _seed_appointment(conn, "Otro Cliente", "11-5555-9999", "tok-oTro-0002", biz_p2_id, "11:00")

    # Turno de Cliente P2 en OTRO business (para rechazo de negocio).
    biz_b_id = conn.execute(
        "INSERT INTO businesses (name, slug, active, pending) VALUES (?, ?, 1, 1)",
        ("Negocio B", "biz-b"),
    ).lastrowid
    conn.execute(
        "INSERT INTO services (business_id, name, price, duration, active) "
        "VALUES (?, 'Servicio B', 1200, 30, 1)",
        (biz_b_id,),
    )
    _seed_appointment(conn, "Cliente P2", "11-5555-1234", "tok-p2-B-0003", biz_b_id, "10:00")

    return {"biz_p2": biz_p2_id, "biz_b": biz_b_id}


def _seed_appointment(
    conn, customer, phone, token, business_id=1, appointment_time="10:00", date="2026-10-12"
):
    """Inserta un turno confirmado con el teléfono NORMALIZADO.

    La reserva real guarda `normalize_phone(phone)` (services/appointments.py);
    el seed replica esa representación para que la verificación por nombre y
    teléfono normalizado de `get_customer_appointments()` coincida.
    """
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    hours, minutes = (int(part) for part in appointment_time.split(":"))
    end_minutes = hours * 60 + minutes + 30
    appointment_end = f"{end_minutes // 60:02d}:{end_minutes % 60:02d}"
    conn.execute(
        "INSERT INTO appointments "
        "(customer_name, phone, service, appointment_date, appointment_time, "
        "appointment_end, duration, business_id, management_token_hash) "
        "VALUES (?, ?, 'Servicio P2', ?, ?, ?, 30, ?, ?)",
        (
            customer,
            normalize_phone(phone),
            date,
            appointment_time,
            appointment_end,
            business_id,
            token_hash,
        ),
    )


def _asserccion_401_idenrica(resp):
    """Verificación del contrato de anti-enumeración (mismo código para todo)."""
    assert resp.status_code == 401
    data = resp.get_json()
    assert data == {"success": False, "code": "verification_required", "error": _TURNOS_ERROR}
    body = resp.get_data(as_text=True)
    assert '"turnos"' not in body
    assert "Cliente P2" not in body
    assert "Otro Cliente" not in body
    assert "Negocio" not in body
    assert "11-5555-1234" not in body


def test_sin_token_devuelve_401_idenrica_sin_pii(turnos_client):
    resp = turnos_client.get(TURNOS_URL)
    _asserccion_401_idenrica(resp)


def test_token_faltante_es_visitante_mismo_codigo(turnos_client):
    resp = turnos_client.get(TURNOS_URL + "&management_token=")
    _asserccion_401_idenrica(resp)


def test_token_inválido_devuelve_401_idenrica(turnos_client):
    resp = turnos_client.get(TURNOS_URL + "&management_token=tok-mente-malo")
    _asserccion_401_idenrica(resp)


def test_token_otro_cliente_devuelve_401_idenrica(turnos_client):
    # Token válido, pero del cliente "Otro Cliente" (mismo business).
    resp = turnos_client.get(TURNOS_URL + "&management_token=tok-oTro-0002")
    _asserccion_401_idenrica(resp)


def test_token_otro_negocio_devuelve_401_idenrica(turnos_client):
    # Token válido del cliente en biz-p2, pero el slug apunta a biz-b
    # (otro business): el filtro por business_id del tenant de la URL rechaza.
    resp = turnos_client.get(
        "/b/biz-b/api/turnos?nombre=Cliente%20P2&telefono=11-5555-1234&management_token=tok-p2-0001"
    )
    _asserccion_401_idenrica(resp)


def test_token_malformado_devuelve_401_idenrica(turnos_client):
    resp = turnos_client.get(TURNOS_URL + "&management_token=12345")
    _asserccion_401_idenrica(resp)


def test_token_excede_límite_de_256_caracteres_devuelve_401(turnos_client):
    resp = turnos_client.get(TURNOS_URL + "&management_token=" + "a" * 257)
    _asserccion_401_idenrica(resp)


def test_token_valido_devuelve_200_solo_turnos_propios_del_mismo_business(turnos_client):
    resp = turnos_client.get(TURNOS_URL + "&management_token=tok-p2-0001")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert "turnos" in data
    assert len(data["turnos"]) == 1
    turno = data["turnos"][0]
    biz_p2_id = turnos_client.application.config["P2_SEED_IDS"]["biz_p2"]
    # La URI de negocio en la URL NO puede escalar: el filtro es business_id.
    assert turno["business_id"] == biz_p2_id
    assert turno["customer_name"] == "Cliente P2"
    # El teléfono se persiste normalizado (sin guiones).
    assert turno["phone"] == "1155551234"
    assert turno["status"] == "confirmed"


def test_ai_sin_token_no_devuelve_pii(turnos_env):
    """Ruta de IA: sin token la herramienta no exfiltra ningún dato.

    Contrato de seguridad (P2-01), sin aserción del mensaje exacto:
    * la petición es rechazada: success == False;
    * no hay datos de turnos en la respuesta;
    * el mensaje de error no contiene PII del cliente (nombre ni teléfono).
    """
    with turnos_env.app_context():
        from services.ai import _execute_buscar_turnos_cliente

        resultado = _execute_buscar_turnos_cliente(
            {"nombre": "Cliente P2", "telefono": "11-5555-1234"},
            business_id=turnos_env.config["P2_SEED_IDS"]["biz_p2"],
            session_id=None,
        )

        assert resultado["success"] is False
        assert "turnos" not in resultado
        error = resultado.get("error", "")
        assert "Cliente P2" not in error
        assert "11-5555-1234" not in error
        assert "management_token" in error.lower()


def test_ai_con_token_si_devuelve_solo_turnos_verificados(turnos_env):
    """Ruta de IA: con token válido devuelve sólo turnos del business.

    Corre sobre la MISMA base SQLite aislada que las pruebas HTTP
    (`turnos_env`), no sobre el app de módulo ni la base del repositorio.
    """
    with turnos_env.app_context():
        from services.ai import _execute_buscar_turnos_cliente

        resultado = _execute_buscar_turnos_cliente(
            {"nombre": "Cliente P2", "telefono": "11-5555-1234", "management_token": "tok-p2-0001"},
            business_id=turnos_env.config["P2_SEED_IDS"]["biz_p2"],
            session_id=None,
        )
        assert resultado["success"] is True
        assert len(resultado["turnos"]) == 1
        assert resultado["turnos"][0]["business_id"] == turnos_env.config["P2_SEED_IDS"]["biz_p2"]
        assert resultado["turnos"][0]["customer_name"] == "Cliente P2"


def test_rate_limit_aplica_antes_de_la_verificacion(turnos_client):
    """La tasa se cuenta sobre todas las peticiones api:turnos, ANTES de que
    el token se evalúe en el servicio. Este run pre-verifica que el
    throttling prioriza la protección sobre la obtención de datos."""
    resp = turnos_client.get("/b/biz-p2/api/turnos?nombre=Cliente%20P2&telefono=11-5555-1234")
    # Primer intento (sin token) -> 401, no es rate limit.
    assert resp.status_code == 401

    for _ in range(64):
        resp = turnos_client.get("/b/biz-p2/api/turnos?nombre=Cliente%20P2&telefono=11-5555-1234")

    assert resp.status_code == 429
    data = resp.get_json()
    assert data["success"] is False


def test_crear_consultar_cancelar_reprogramar_con_token_propio():
    """Cadena de negocio: crear -> consultar -> cancelar -> reprogramar.

    COBERTURA PENDIENTE (no verificada en esta suite SQLite):
    - La herramienta de creación de turnos ``crear_turno_declaration`` no
      existe en la implementación SQLite, por lo que el paso "crear" no está
      disponible aquí.
    - Cancelar y reprogramar son operaciones de escritura sobre la tabla de
      turnos con la misma regla de verificación de posesión; tampoco están
      cubiertas en SQLite.
    - Las operaciones de crear, cancelar y reprogramar pertenecen a la suite
      PostgreSQL (tests_pg/), donde la base real ejecuta esas transacciones.

    El skip se declara ANTES de cualquier import de símbolos inexistentes para
    no convertir un error de importación en una falsa cobertura.
    """
    pytest.skip(
        "Cobertura pendiente: crear -> consultar -> cancelar -> reprogramar. "
        "crear_turno_declaration no está disponible en la implementación "
        "SQLite; operaciones de escritura verificadas en tests_pg/."
    )


if __name__ == "__main__":
    raise RuntimeError("no ejecutar como script; pytest solo")
