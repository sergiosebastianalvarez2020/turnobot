PRAGMA foreign_keys = OFF;

BEGIN;

-- ============================================================
-- ETAPA 2 - IDEMPOTENCIA DE create_appointment
-- ============================================================
--
-- idempotency_key: clave opaca que el cliente genera al RENDERIZAR el
-- formulario (o el boton de reservar) y reenvia en cada intento del mismo
-- render. Mismo patron que redemptions.idempotency_key (migracion 010).
--
-- Absorbe double-click, refresh, back, reintento HTTP y dos solicitudes
-- simultaneas con la misma clave: un segundo intento con la misma clave devuelve
-- el turno original en lugar de fallar con 'occupied'. Dos reservas legitimas
-- usan claves distintas (clave nueva por render) y se registran normal.
--
-- NULL = turno creado sin clave (alta manual del admin, asistente de IA): no
-- participa del indice y su comportamiento no cambia. Por eso el indice es
-- PARCIAL en lugar de un UNIQUE sobre la columna.
--
-- El management_token sigue siendo un secreto de un solo uso generado por el
-- servidor (solo se persiste su SHA-256), asi que un replay devuelve el
-- appointment_id pero NO puede reconstruir el token.

ALTER TABLE appointments ADD COLUMN idempotency_key TEXT;

CREATE UNIQUE INDEX unique_appointment_idempotency_key
ON appointments (business_id, idempotency_key)
WHERE idempotency_key IS NOT NULL;

COMMIT;

PRAGMA foreign_keys = ON;
