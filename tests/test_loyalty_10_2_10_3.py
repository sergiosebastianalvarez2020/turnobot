import unittest
import sys
from concurrent.futures import ThreadPoolExecutor
import re
from pathlib import Path
from datetime import date, timedelta

from database.database import get_connection
from services import loyalty
sys.path.insert(0, str(Path(__file__).parent))
from test_loyalty import LoyaltyBase


class RewardsAndRetentionTests(LoyaltyBase):
    def setUp(self):
        super().setUp()
        self._execute("INSERT OR IGNORE INTO loyalty_settings (business_id, enabled, points_per_completed_appointment) VALUES (1,1,1)")
        self._execute("UPDATE loyalty_settings SET enabled=1 WHERE business_id=1")
        account = loyalty.get_account(1, "3815000001")
        if not account:
            self._execute("INSERT INTO loyalty_accounts (business_id, customer_phone, points_balance) VALUES (1,'3815000001',100)")
            self._execute("INSERT INTO points_ledger (business_id, account_id, delta, type, reason) VALUES (1, (SELECT id FROM loyalty_accounts WHERE business_id=1 AND customer_phone='3815000001'), 100, 'adjust', 'test')")
        self.account = loyalty.get_account(1, "3815000001")

    def test_reward_redeem_is_idempotent_and_ledger_linked(self):
        self.assertTrue(loyalty.save_reward(1, None, "Café", "", 80)["success"])
        reward = loyalty.list_rewards(1)[0]
        first = loyalty.redeem(1, self.account["id"], reward["id"], "same-key")
        second = loyalty.redeem(1, self.account["id"], reward["id"], "same-key")
        self.assertTrue(first["success"], first)
        self.assertTrue(second["success"])
        self.assertEqual(second["reason"], "already_processed")
        connection = get_connection()
        try:
            row = connection.execute("SELECT COUNT(*) c FROM redemptions WHERE business_id=1 AND idempotency_key='same-key'").fetchone()
        finally:
            connection.close()
        self.assertEqual(row["c"], 1)
        self.assertEqual(loyalty.get_balance(1, "3815000001"), 20)

    def test_retention_uses_normalized_phone_and_2_60_rule(self):
        old = (date.today() - timedelta(days=61)).isoformat()
        for day in (old, (date.today() - timedelta(days=90)).isoformat()):
            self._execute("INSERT INTO appointments (customer_name, phone, service, appointment_date, appointment_time, appointment_end, duration, status, business_id) VALUES (?,?,?,?,?,? ,60,'completed',1)", ("Cliente", "+54 381-500-0001", "Corte", day, "10:00", "11:00"))
        candidates = loyalty.retention_candidates(1)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["customer_phone"], "543815000001")

    def test_concurrent_redeems_cannot_overspend(self):
        self.assertTrue(loyalty.save_reward(1, None, "Grande", "", 80)["success"])
        reward = loyalty.list_rewards(1)[0]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda key: loyalty.redeem(1, self.account["id"], reward["id"], key), ("a", "b")))
        self.assertEqual(sum(result["success"] for result in results), 1)
        self.assertEqual(loyalty.get_balance(1, "3815000001"), 20)
        self.assertEqual(len([row for row in self._ledger(1, self.account["id"]) if row["type"] == "redeem"]), 1)

    def test_reward_validation_and_inactive_reward(self):
        self.assertFalse(loyalty.save_reward(1, None, "", "", 0)["success"])
        self.assertTrue(loyalty.save_reward(1, None, "Inactiva", "", 80, active=False)["success"])
        reward = loyalty.list_rewards(1)[0]
        self.assertFalse(loyalty.redeem(1, self.account["id"], reward["id"], "inactive")["success"])

    def test_admin_http_csrf_and_tenant_scope(self):
        token = self.csrf_token
        response = self.client.post("/admin/fidelizacion/recompensas", data={"name": "HTTP", "points_cost": "10", "csrf_token": token})
        self.assertEqual(response.status_code, 302)
        bad_csrf = self.client.post("/admin/fidelizacion/recompensas", data={"name": "No", "points_cost": "10"})
        self.assertEqual(bad_csrf.status_code, 400)
        reward = loyalty.list_rewards(1)[0]
        denied = self.client.post(f"/b/business-b/admin/fidelizacion/recompensas/{reward['id']}/estado", data={"csrf_token": token, "active": "0"})
        self.assertIn(denied.status_code, (302, 403, 404))

    def test_off_disables_redeem(self):
        self._execute("UPDATE loyalty_settings SET enabled=0 WHERE business_id=1")
        self.assertEqual(loyalty.redeem(1, self.account["id"], 999, "off")["reason"], "disabled")

    def test_http_historial_scoped_y_permisos(self):
        loyalty.save_reward(1, None, "Histórica", "", 10)
        reward = loyalty.list_rewards(1)[0]
        loyalty.redeem(1, self.account["id"], reward["id"], "history-key")
        page = self.client.get("/admin/fidelizacion/recompensas")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Histórica", page.text)
        self.assertIn("Historial de canjes", page.text)
        other_tenant = self.client.get("/b/business-b/admin/fidelizacion/recompensas")
        self.assertIn(other_tenant.status_code, (302, 403, 404))
        with self.client.session_transaction() as session:
            session["user_id"] = self.actor_user_id
        no_permission = self.client.get("/admin/fidelizacion/recompensas")
        self.assertIn(no_permission.status_code, (302, 403))

    def test_http_recuperacion_on_off_y_csrf(self):
        old = (date.today() - timedelta(days=61)).isoformat()
        for index, status in enumerate(("completed", "completed", "cancelled", "no_show")):
            self._execute("INSERT INTO appointments (customer_name, phone, service, appointment_date, appointment_time, appointment_end, duration, status, business_id) VALUES (?,?,?,?,?,? ,60,?,1)", ("Recuperable", "3815000099", "Corte", old, "10:0" + str(index), "11:0" + str(index), status))
        on_page = self.client.get("/admin/fidelizacion/recompensas")
        self.assertIn("Clientes recuperables", on_page.text)
        self.assertIn("Recuperable", on_page.text)
        self._execute("UPDATE loyalty_settings SET enabled=0 WHERE business_id=1")
        off_page = self.client.get("/admin/fidelizacion/recompensas")
        self.assertNotIn("Clientes recuperables", off_page.text)
        bad = self.client.post("/admin/fidelizacion/recompensas", data={"name": "x", "points_cost": "1"})
        self.assertEqual(bad.status_code, 400)


if __name__ == "__main__":
    unittest.main()
