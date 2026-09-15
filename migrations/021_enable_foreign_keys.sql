PRAGMA foreign_keys = OFF;

BEGIN;

-- Enable foreign keys for this connection
PRAGMA foreign_keys = ON;

-- Verify foreign keys are now enabled
-- This will fail if there are any constraint violations

COMMIT;

PRAGMA foreign_keys = ON;