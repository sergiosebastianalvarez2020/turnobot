-- Migracion 003: incorpora el negocio actual y relaciones tenant-aware.

PRAGMA foreign_keys = OFF;

BEGIN;

CREATE TABLE IF NOT EXISTS businesses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE
);

INSERT INTO businesses (id, name, slug)
SELECT 1, business_name, 'el-corte'
FROM business_settings
WHERE id = 1
  AND NOT EXISTS (SELECT 1 FROM businesses WHERE id = 1);

CREATE TABLE business_settings_new (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    business_name TEXT DEFAULT 'Mi negocio',
    slot_duration INTEGER NOT NULL DEFAULT 60,
    break_between_slots INTEGER NOT NULL DEFAULT 0,
    business_type TEXT NOT NULL DEFAULT 'Barberia',
    business_initials TEXT NOT NULL DEFAULT 'EC',
    business_description TEXT NOT NULL DEFAULT 'Barberia masculina',
    timezone TEXT NOT NULL DEFAULT 'America/Argentina/Buenos_Aires',
    business_id INTEGER NOT NULL DEFAULT 1,
    UNIQUE (business_id),
    FOREIGN KEY (business_id) REFERENCES businesses(id)
);

INSERT INTO business_settings_new (
    id, business_name, slot_duration, break_between_slots,
    business_type, business_initials, business_description, timezone,
    business_id
)
SELECT id, business_name, slot_duration, break_between_slots,
       business_type, business_initials, business_description, timezone,
       1
FROM business_settings;

CREATE TABLE services_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    price REAL NOT NULL DEFAULT 0,
    duration INTEGER NOT NULL DEFAULT 60,
    active INTEGER NOT NULL DEFAULT 1,
    business_id INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (business_id) REFERENCES businesses(id)
);

INSERT INTO services_new (id, name, price, duration, active, business_id)
SELECT id, name, price, duration, active, 1
FROM services;

CREATE INDEX idx_services_business ON services_new (business_id);

CREATE TABLE weekly_schedules_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day_of_week INTEGER NOT NULL,
    is_open INTEGER NOT NULL DEFAULT 0,
    morning_start TEXT,
    morning_end TEXT,
    afternoon_start TEXT,
    afternoon_end TEXT,
    business_id INTEGER NOT NULL DEFAULT 1,
    UNIQUE (business_id, day_of_week),
    FOREIGN KEY (business_id) REFERENCES businesses(id)
);

INSERT INTO weekly_schedules_new (
    id, day_of_week, is_open, morning_start, morning_end,
    afternoon_start, afternoon_end, business_id
)
SELECT id, day_of_week, is_open, morning_start, morning_end,
       afternoon_start, afternoon_end, 1
FROM weekly_schedules;

CREATE INDEX idx_schedules_business ON weekly_schedules_new (business_id);

CREATE TABLE appointments_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_name TEXT NOT NULL,
    phone TEXT,
    service TEXT NOT NULL,
    appointment_date TEXT NOT NULL,
    appointment_time TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'confirmed',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    business_id INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (business_id) REFERENCES businesses(id)
);

INSERT INTO appointments_new (
    id, customer_name, phone, service, appointment_date,
    appointment_time, status, created_at, business_id
)
SELECT id, customer_name, phone, service, appointment_date,
       appointment_time, status, created_at, 1
FROM appointments;

DROP INDEX IF EXISTS unique_confirmed_appointment_slot;
DROP INDEX IF EXISTS idx_appointments_phone;
DROP INDEX IF EXISTS idx_appointments_date_status;
DROP INDEX IF EXISTS idx_appointments_status;

CREATE UNIQUE INDEX unique_confirmed_appointment_slot
ON appointments_new (business_id, appointment_date, appointment_time)
WHERE status = 'confirmed';
CREATE INDEX idx_appointments_phone ON appointments_new (phone);
CREATE INDEX idx_appointments_date_status
ON appointments_new (appointment_date, status);
CREATE INDEX idx_appointments_status ON appointments_new (status);
CREATE INDEX idx_appointments_business ON appointments_new (business_id);

DROP TABLE appointments;
DROP TABLE weekly_schedules;
DROP TABLE services;
DROP TABLE business_settings;

ALTER TABLE appointments_new RENAME TO appointments;
ALTER TABLE weekly_schedules_new RENAME TO weekly_schedules;
ALTER TABLE services_new RENAME TO services;
ALTER TABLE business_settings_new RENAME TO business_settings;

COMMIT;

PRAGMA foreign_keys = ON;
