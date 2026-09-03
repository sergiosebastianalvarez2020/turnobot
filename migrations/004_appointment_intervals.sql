-- Migracion 004: Intervalos de turnos.
-- Agrega a appointments la duracion capturada al reservar (duration)
-- y el fin real del turno (appointment_end), conservando appointment_time
-- como hora de inicio (y por compatibilidad).

-- Integridad historica: duration y appointment_end quedan fijados al momento
-- de la reserva. Un cambio posterior de service.duration NO altera los
-- turnos existentes.

PRAGMA foreign_keys = OFF;

BEGIN;

CREATE TABLE appointments_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_name TEXT NOT NULL,
    phone TEXT,
    service TEXT NOT NULL,
    appointment_date TEXT NOT NULL,
    appointment_time TEXT NOT NULL,
    appointment_end TEXT NOT NULL,
    duration INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'confirmed',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    business_id INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (business_id) REFERENCES businesses(id)
);

-- Backfill:
--   duration = duracion del servicio del mismo negocio (services.duration)
--   fallback  = slot_duration del negocio (business_settings)
--   fallback 2 = 60 (valor por defecto del proyecto)
--   appointment_end = appointment_time + duration
-- Los turnos cuyo servicio no exista o sea de otro negocio NO se eliminan:
-- se conservan con el fallback definido por la configuracion existente.
INSERT INTO appointments_new (
    id, customer_name, phone, service, appointment_date,
    appointment_time, appointment_end, duration, status, created_at, business_id
)
SELECT
    a.id,
    a.customer_name,
    a.phone,
    a.service,
    a.appointment_date,
    a.appointment_time,
    printf('%02d:%02d',
        (substr(a.appointment_time, 1, 2) * 60
         + substr(a.appointment_time, 4, 2)
         + COALESCE(
             (SELECT s.duration FROM services s
              WHERE s.name = a.service AND s.business_id = a.business_id),
             (SELECT bs.slot_duration FROM business_settings bs
              WHERE bs.business_id = a.business_id),
             60
         )) / 60,
        (substr(a.appointment_time, 1, 2) * 60
         + substr(a.appointment_time, 4, 2)
         + COALESCE(
             (SELECT s.duration FROM services s
              WHERE s.name = a.service AND s.business_id = a.business_id),
             (SELECT bs.slot_duration FROM business_settings bs
              WHERE bs.business_id = a.business_id),
             60
         )) % 60),
    COALESCE(
        (SELECT s.duration FROM services s
         WHERE s.name = a.service AND s.business_id = a.business_id),
        (SELECT bs.slot_duration FROM business_settings bs
         WHERE bs.business_id = a.business_id),
        60
    ),
    a.status,
    a.created_at,
    a.business_id
FROM appointments AS a;

DROP INDEX IF EXISTS unique_confirmed_appointment_slot;
DROP INDEX IF EXISTS idx_appointments_phone;
DROP INDEX IF EXISTS idx_appointments_date_status;
DROP INDEX IF EXISTS idx_appointments_status;
DROP INDEX IF EXISTS idx_appointments_business;

-- Se conserva el UNIQUE parcial como defensa en profundidad contra dos
-- reservas con identico inicio (business_id + fecha + hora exactas).
CREATE UNIQUE INDEX unique_confirmed_appointment_slot
ON appointments_new (business_id, appointment_date, appointment_time)
WHERE status = 'confirmed';
CREATE INDEX idx_appointments_phone ON appointments_new (phone);
CREATE INDEX idx_appointments_date_status
ON appointments_new (appointment_date, status);
CREATE INDEX idx_appointments_status ON appointments_new (status);
CREATE INDEX idx_appointments_business ON appointments_new (business_id);

DROP TABLE appointments;

ALTER TABLE appointments_new RENAME TO appointments;

COMMIT;

PRAGMA foreign_keys = ON;