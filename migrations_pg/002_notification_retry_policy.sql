ALTER TABLE notification_log
    ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE notification_log
    ADD COLUMN next_retry_at TIMESTAMPTZ;
CREATE INDEX idx_notification_log_retry
    ON notification_log (status, next_retry_at, id);
