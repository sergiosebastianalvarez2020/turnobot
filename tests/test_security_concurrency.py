import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import database.database as database
from services import appointments, notifications, platform


class AtomicSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = database.DATABASE_PATH
        database.DATABASE_PATH = Path(self.tmp.name) / "test.db"
        database.init_database()

    def tearDown(self):
        database.DATABASE_PATH = self.old_path
        self.tmp.cleanup()

    def _invitation(self):
        result = platform.provision_business("Demo", "demo", "owner@example.com")
        approved = platform.approve_business(result["business_id"])
        return result["business_id"], approved["invitation_url"].rsplit("/", 1)[-1], result["user_id"]

    def test_same_invitation_only_one_concurrent_acceptance(self):
        business_id, token, user_id = self._invitation()
        barrier = threading.Barrier(2)
        results = []

        def worker(password):
            barrier.wait()
            results.append(platform.accept_invitation(business_id, token, password))

        threads = [threading.Thread(target=worker, args=(f"Password-{i}-secure",)) for i in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(sum(result["success"] for result in results), 1)
        self.assertEqual(sum(result["reason"] == "invalid_token" for result in results), 1)
        connection = database.get_connection()
        row = connection.execute(
            "SELECT used_at, active, password_hash FROM users JOIN invitations ON invitations.user_id = users.id WHERE users.id = ?",
            (user_id,),
        ).fetchone()
        connection.close()
        self.assertIsNotNone(row["used_at"])
        self.assertEqual(row["active"], 1)
        self.assertTrue(row["password_hash"])

    def test_reinvite_revokes_old_and_preserves_history(self):
        business_id, token_a, user_id = self._invitation()
        token_b, _ = platform.resend_invitation(business_id, user_id, "owner@example.com")
        self.assertEqual(platform.accept_invitation(business_id, token_a, "Password-A-secure")["reason"], "invalid_token")
        self.assertTrue(platform.accept_invitation(business_id, token_b, "Password-B-secure")["success"])
        self.assertEqual(platform.accept_invitation(business_id, token_b, "Password-C-secure")["reason"], "invalid_token")
        connection = database.get_connection()
        rows = connection.execute(
            "SELECT revoked_at, used_at FROM invitations WHERE business_id = ? ORDER BY id", (business_id,)
        ).fetchall()
        connection.close()
        self.assertEqual(len(rows), 2)
        self.assertIsNotNone(rows[0]["revoked_at"])
        self.assertIsNotNone(rows[1]["used_at"])

    def test_email_claim_allows_one_worker_and_retry_after_failure(self):
        date = (datetime.now().date() + timedelta(days=1)).isoformat()
        appointment = appointments.create_appointment("Ana", "3815000000", "Corte", date, "09:00", 1, email="ana@example.com")
        apt = dict(appointment)
        apt["id"] = appointment["appointment_id"]
        calls = []
        lock = threading.Lock()

        def fake_send(*_args):
            with lock: calls.append(1)
            return True, ""

        results = []
        with mock.patch.dict("os.environ", {"SMTP_HOST": "smtp.test"}), mock.patch.object(notifications, "_send_email", side_effect=fake_send):
            threads = [threading.Thread(target=lambda: results.append(notifications.send_confirmation_email(1, apt, force=True))) for _ in range(2)]
            for thread in threads: thread.start()
            for thread in threads: thread.join()
        self.assertEqual(sum(result[0] for result in results), 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(database.get_notification_state_scoped(1, apt["id"], notifications.CONFIRMATION, "email")["status"], "sent")
