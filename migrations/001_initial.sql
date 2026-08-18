-- Migración 001: Esquema inicial
-- Tablas: appointments, business_settings, weekly_schedules, services


-- ============================================================
-- TURNOS
-- ============================================================

CREATE TABLE IF NOT EXISTS appointments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_name TEXT NOT NULL,
    phone TEXT,
    service TEXT NOT NULL,
    appointment_date TEXT NOT NULL,
    appointment_time TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'confirmed',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS unique_confirmed_appointment_slot
ON appointments (appointment_date, appointment_time)
WHERE status = 'confirmed';


-- ============================================================
-- CONFIGURACIÓN DEL NEGOCIO
-- ============================================================

CREATE TABLE IF NOT EXISTS business_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    business_name TEXT DEFAULT 'Mi negocio',
    slot_duration INTEGER NOT NULL DEFAULT 60,
    break_between_slots INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO business_settings (id, business_name, slot_duration, break_between_slots)
VALUES (1, 'El Corte', 60, 0);


-- ============================================================
-- HORARIOS SEMANALES (0=lun, 6=dom)
-- ============================================================

CREATE TABLE IF NOT EXISTS weekly_schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day_of_week INTEGER NOT NULL,
    is_open INTEGER NOT NULL DEFAULT 0,
    morning_start TEXT,
    morning_end TEXT,
    afternoon_start TEXT,
    afternoon_end TEXT,
    UNIQUE(day_of_week)
);

INSERT OR IGNORE INTO weekly_schedules (day_of_week, is_open, morning_start, morning_end, afternoon_start, afternoon_end) VALUES (0, 1, '09:00', '13:00', '15:00', '20:00');
INSERT OR IGNORE INTO weekly_schedules (day_of_week, is_open, morning_start, morning_end, afternoon_start, afternoon_end) VALUES (1, 1, '09:00', '13:00', '15:00', '20:00');
INSERT OR IGNORE INTO weekly_schedules (day_of_week, is_open, morning_start, morning_end, afternoon_start, afternoon_end) VALUES (2, 1, '09:00', '13:00', '15:00', '20:00');
INSERT OR IGNORE INTO weekly_schedules (day_of_week, is_open, morning_start, morning_end, afternoon_start, afternoon_end) VALUES (3, 1, '09:00', '13:00', '15:00', '20:00');
INSERT OR IGNORE INTO weekly_schedules (day_of_week, is_open, morning_start, morning_end, afternoon_start, afternoon_end) VALUES (4, 1, '09:00', '13:00', '15:00', '20:00');
INSERT OR IGNORE INTO weekly_schedules (day_of_week, is_open, morning_start, morning_end, afternoon_start, afternoon_end) VALUES (5, 1, '09:00', '13:00', NULL, NULL);
INSERT OR IGNORE INTO weekly_schedules (day_of_week, is_open, morning_start, morning_end, afternoon_start, afternoon_end) VALUES (6, 0, NULL, NULL, NULL, NULL);


-- ============================================================
-- SERVICIOS
-- ============================================================

CREATE TABLE IF NOT EXISTS services (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    price REAL NOT NULL DEFAULT 0,
    duration INTEGER NOT NULL DEFAULT 60,
    active INTEGER NOT NULL DEFAULT 1
);

INSERT OR IGNORE INTO services (id, name, price, duration, active) VALUES (1, 'Corte', 10000, 30, 1);
INSERT OR IGNORE INTO services (id, name, price, duration, active) VALUES (2, 'Corte + barba', 15000, 50, 1);
INSERT OR IGNORE INTO services (id, name, price, duration, active) VALUES (3, 'Barba', 7000, 20, 1);
