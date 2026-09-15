PRAGMA foreign_keys = OFF;

BEGIN;

-- Check for orphaned records before enabling foreign keys
-- Appointments with invalid business_id
SELECT 'orphaned appointments' as check_type, COUNT(*) as count
FROM appointments a
LEFT JOIN businesses b ON a.business_id = b.id
WHERE b.id IS NULL;

-- Appointments with invalid resource_id
SELECT 'orphaned appointments resource' as check_type, COUNT(*) as count
FROM appointments a
LEFT JOIN resources r ON a.resource_id = r.id
WHERE a.resource_id IS NOT NULL AND r.id IS NULL;

-- Services with invalid business_id
SELECT 'orphaned services' as check_type, COUNT(*) as count
FROM services s
LEFT JOIN businesses b ON s.business_id = b.id
WHERE b.id IS NULL;

-- Resources with invalid business_id
SELECT 'orphaned resources' as check_type, COUNT(*) as count
FROM resources r
LEFT JOIN businesses b ON r.business_id = b.id
WHERE b.id IS NULL;

-- Loyalty accounts with invalid business_id
SELECT 'orphaned loyalty accounts' as check_type, COUNT(*) as count
FROM loyalty_accounts la
LEFT JOIN businesses b ON la.business_id = b.id
WHERE b.id IS NULL;

-- Points ledger with invalid business_id or account_id
SELECT 'orphaned ledger business' as check_type, COUNT(*) as count
FROM points_ledger pl
LEFT JOIN businesses b ON pl.business_id = b.id
WHERE b.id IS NULL;

SELECT 'orphaned ledger account' as check_type, COUNT(*) as count
FROM points_ledger pl
LEFT JOIN loyalty_accounts la ON pl.account_id = la.id
WHERE la.id IS NULL;

-- Conversation sessions with invalid business_id
SELECT 'orphaned conversation sessions' as check_type, COUNT(*) as count
FROM conversation_sessions cs
LEFT JOIN businesses b ON cs.business_id = b.id
WHERE b.id IS NULL;

-- Conversation messages with invalid session_id
SELECT 'orphaned conversation messages' as check_type, COUNT(*) as count
FROM conversation_messages cm
LEFT JOIN conversation_sessions cs ON cm.session_id = cs.id
WHERE cs.id IS NULL;

-- Knowledge entries with invalid business_id
SELECT 'orphaned knowledge' as check_type, COUNT(*) as count
FROM business_knowledge bk
LEFT JOIN businesses b ON bk.business_id = b.id
WHERE b.id IS NULL;

-- Notification log with invalid business_id or appointment_id
SELECT 'orphaned notification log business' as check_type, COUNT(*) as count
FROM notification_log nl
LEFT JOIN businesses b ON nl.business_id = b.id
WHERE b.id IS NULL;

SELECT 'orphaned notification log appointment' as check_type, COUNT(*) as count
FROM notification_log nl
LEFT JOIN appointments a ON nl.appointment_id = a.id
WHERE a.id IS NULL;

-- Business settings with invalid business_id (should only be one per business)
SELECT 'duplicate business settings' as check_type, COUNT(*) as count
FROM business_settings
GROUP BY business_id
HAVING COUNT(*) > 1;

COMMIT;