import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from test_loyalty import LoyaltyBase
from services import product


class ProductStage11Tests(LoyaltyBase):
    def test_summary_is_scoped_and_reuses_existing_data(self):
        self._execute("INSERT INTO appointments (customer_name,phone,service,appointment_date,appointment_time,appointment_end,duration,status,business_id) VALUES ('A','3815000001','Corte','2020-01-01','10:00','11:00',60,'completed',1)")
        summary = product.get_product_summary(1)
        self.assertEqual(summary["completed"], 1)
        self.assertIn("recurring", summary)
        self.assertNotIn("business_id", summary)

    def test_onboarding_detects_missing_service(self):
        state = product.get_onboarding_state(1, {"business_name": "Mi negocio"}, [])
        self.assertFalse(state["has_service"])
        self.assertTrue(state["business_configured"])

    def test_admin_shows_onboarding_and_metrics(self):
        response = self.client.get("/admin")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Resumen de tu negocio", response.text)
        self.assertIn('id="configuracion"', response.text)
        self.assertTrue("Primeros pasos" in response.text or "No hay turnos" in response.text)
