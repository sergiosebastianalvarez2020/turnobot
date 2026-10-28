BEGIN IMMEDIATE;

ALTER TABLE notification_log
    ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE notification_log
    ADD COLUMN next_retry_at TEXT;
CREATE INDEX idx_notification_log_retry
    ON notification_log (status, next_retry_at, id);

COMMIT;
