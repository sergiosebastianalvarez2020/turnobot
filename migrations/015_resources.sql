-- Migracion 015: Recursos reservables por negocio.
--
-- Crea la tabla `resources` (ej.: Cancha 1, Cancha 2, Sala A) y agrega la
-- columna nullable `appointments.resource_id`.
--
-- Compatibilidad:
--   * Un negocio SIN recursos sigue funcionando exactamente igual:
--     sus turnos tienen resource_id NULL y bloquean el negocio completo.
--   * Un negocio CON recursos: cada turno ocupa su recurso; la garantia
--     contra doble reserva se mantiene via verificacion transaccional de
--     solapamiento (BEGIN IMMEDIATE) y via indice unico parcial:
--       - resource_id IS NULL -> unique (business_id, date, time) [bloqueo global]
--       - resource_id NOT NULL -> unique (business_id, resource_id, date, time)
--
-- Se reemplaza el indice unico existente para NO impedir reservas
-- simultaneas en recursos distintos.

PRAGMA foreign_keys = OFF;

BEGIN;

CREATE TABLE resources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    UNIQUE (business_id, name)
);

CREATE INDEX idx_resources_business ON resources (business_id);

ALTER TABLE appointments ADD COLUMN resource_id INTEGER REFERENCES resources(id);

-- Indices únicos reemplazados (drop por nombre historico + recreate parcial).
DROP INDEX IF EXISTS unique_confirmed_appointment_slot;

CREATE UNIQUE INDEX unique_confirmed_appointment_slot
ON appointments (business_id, appointment_date, appointment_time)
WHERE status = 'confirmed' AND resource_id IS NULL;

CREATE UNIQUE INDEX unique_confirmed_appointment_resource_slot
ON appointments (business_id, resource_id, appointment_date, appointment_time)
WHERE status = 'confirmed' AND resource_id IS NOT NULL;

COMMIT;

PRAGMA foreign_keys = ON;
