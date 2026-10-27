import smtplib
from datetime import UTC, datetime, timedelta
from unittest import mock

import database.database as database
from services import notifications


class SMTPClient:
    def __init__(self, *args, **kwargs):
        self.login_calls = []
        self.sendmail_calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args, **kwargs):
        return False

    def ehlo(self):
        return 250, b"ok"

    def starttls(self):
        raise smtplib.SMTPNotSupportedError("STARTTLS unavailable")

    def login(self, user, password):
        self.login_calls.append((user, password))

    def sendmail(self, from_addr, to_addrs, message):
        self.sendmail_calls.append((from_addr, to_addrs, message))


def test_retry_backoff_is_exponential_and_capped():
    assert notifications.retry_delay_seconds(0) == 15 * 60
    assert notifications.retry_delay_seconds(1) == 30 * 60
    assert notifications.retry_delay_seconds(2) == 60 * 60
    assert notifications.retry_delay_seconds(3) == 2 * 60 * 60
    assert notifications.retry_delay_seconds(4) == 4 * 60 * 60
    assert notifications.retry_delay_seconds(20) == 4 * 60 * 60


def test_sqlite_migration_adds_retry_state_and_query_runs(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_BACKEND", "sqlite")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "appointments.db")

    database.apply_migrations()

    connection = database.get_connection()
    try:
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(notification_log)")
        }
    finally:
        connection.close()

    assert {"retry_count", "next_retry_at"} <= columns

    connection = database.get_connection()
    connection.execute("INSERT INTO businesses (id, name, slug) VALUES (2, 'Test', 'test')")
    connection.execute(
        "INSERT INTO business_settings (business_id, notifications_enabled) VALUES (2, 1)"
    )
    connection.execute(
        """INSERT INTO appointments
           (id, customer_name, service, appointment_date, appointment_time,
            appointment_end, duration, business_id)
           VALUES (1, 'Test customer', 'Service', '2030-01-02', '09:00',
                   '10:00', 60, 2)"""
    )
    connection.commit()
    connection.close()

    assert database.claim_notification_scoped(
        1, 2, "confirmation", "email", "customer@example.com"
    )
    future_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat(sep=" ")
    database.upsert_notification_log_scoped(
        1,
        2,
        "confirmation",
        "email",
        "customer@example.com",
        status="failed",
        error="temporary failure",
        next_retry_at=future_at,
    )
    assert database.list_failed_notifications_scoped(business_id=2) == []
    assert not database.claim_notification_scoped(
        1, 2, "confirmation", "email", "customer@example.com"
    )

    due_at = (datetime.now(UTC) - timedelta(minutes=1)).isoformat(sep=" ")
    connection = database.get_connection()
    connection.execute(
        "UPDATE notification_log SET next_retry_at = ? WHERE appointment_id = 1",
        (due_at,),
    )
    connection.commit()
    connection.close()

    failed = database.list_failed_notifications_scoped(business_id=2)
    assert len(failed) == 1
    assert database.claim_notification_scoped(
        1, 2, "confirmation", "email", "customer@example.com"
    )
    state = database.get_notification_state_scoped(2, 1, "confirmation", "email")
    assert state["retry_count"] == 1

    database.upsert_notification_log_scoped(
        1, 2, "confirmation", "email", "customer@example.com", status="sent"
    )
    assert not database.claim_notification_scoped(
        1, 2, "confirmation", "email", "customer@example.com"
    )

    connection = database.get_connection()
    connection.execute(
        """UPDATE notification_log SET status = 'failed', retry_count = ?,
                   next_retry_at = NULL WHERE appointment_id = 1""",
        (database.MAX_NOTIFICATION_RETRIES,),
    )
    connection.commit()
    connection.close()
    assert database.list_failed_notifications_scoped(business_id=2) == []

    connection = database.get_connection()
    connection.execute(
        "UPDATE business_settings SET notifications_enabled = 0 WHERE business_id = 2"
    )
    connection.execute(
        """UPDATE notification_log SET status = 'failed', retry_count = 0,
                   next_retry_at = NULL WHERE appointment_id = 1"""
    )
    connection.commit()
    connection.close()
    assert database.list_failed_notifications_scoped(business_id=2) == []


def test_missing_smtp_configuration_does_not_open_connection(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    with mock.patch.object(notifications.smtplib, "SMTP") as smtp:
        sent, error = notifications._send_email(
            "customer@example.com", "subject", "<p>hi</p>", "hi"
        )

    assert not sent
    assert error == "SMTP no configurado"
    smtp.assert_not_called()


def test_starttls_failure_never_authenticates_or_sends(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USER", "test-user")
    monkeypatch.setenv("SMTP_PASSWORD", "test-secret")
    monkeypatch.setenv("EMAIL_FROM", "noreply@example.com")
    client = SMTPClient()

    with mock.patch.object(notifications.smtplib, "SMTP", return_value=client):
        sent, error = notifications._send_email(
            "customer@example.com", "subject", "<p>hi</p>", "hi"
        )

    assert not sent
    assert "STARTTLS unavailable" in error
    assert "test-secret" not in error
    assert client.login_calls == []
    assert client.sendmail_calls == []
