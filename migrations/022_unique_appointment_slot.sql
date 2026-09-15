PRAGMA foreign_keys = OFF;

BEGIN;

-- Create a new unique index that prevents double bookings at the same time
-- for the same business, date, time, and resource (or global if no resource)
-- This prevents race conditions at the database level.

-- First, drop the existing unique index
DROP INDEX IF EXISTS unique_confirmed_appointment_slot;

-- Create a new unique index that includes resource_id (NULL = global/no resource)
-- This prevents two confirmed appointments at the exact same time for the same resource
CREATE UNIQUE INDEX unique_confirmed_appointment_slot
ON appointments (business_id, appointment_date, appointment_time, COALESCE(resource_id, -1))
WHERE status = 'confirmed';

COMMIT;

PRAGMA foreign_keys = ON;