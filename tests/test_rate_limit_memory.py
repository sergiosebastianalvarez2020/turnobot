"""Pruebas de P.9-F2: rate limiting acotado y thread-safe.

- Poda de claves con timestamps viejos y de claves vacías.
- Acotado por RATE_LIMIT_MAX_KEYS (expulsión FIFO).
- Concurrencia: acceso multi-thread sin excepciones y conteo acotado.
- Sin sleeps: se usa `now` inyectable en _prune_rate_limit_state.
"""

import json
import threading
import unittest
from collections import deque

import app as application


class RateLimitMemoryTests(unittest.TestCase):

    def setUp(self):
        application.rate_limit_state.clear()

    def tearDown(self):
        application.rate_limit_state.clear()

    def test_sweep_removes_stale_and_empty_keys(self):
        state = application.rate_limit_state
        state["fresh"] = deque([142.0, 150.0])
        state["stale"] = deque([130.0])
        state["empty"] = deque()
        application._prune_rate_limit_state(now=200.0)
        self.assertIn("fresh", state)
        self.assertNotIn("stale", state)
        self.assertNotIn("empty", state)
        self.assertEqual(list(state["fresh"]), [142.0, 150.0])

    def test_sweep_enforces_max_keys_fifo(self):
        state = application.rate_limit_state
        state["key1"] = deque([150.0])
        state["key2"] = deque([160.0])
        state["key3"] = deque([170.0])
        state["key4"] = deque([180.0])
        state["key5"] = deque([190.0])
        application._prune_rate_limit_state(now=200.0, max_keys=3)
        self.assertLessEqual(len(state), 3)
        self.assertNotIn("key1", state)
        self.assertNotIn("key2", state)
        self.assertIn("key3", state)
        self.assertIn("key4", state)
        self.assertIn("key5", state)

    def test_sweep_removes_stale_before_fifo_eviction(self):
        state = application.rate_limit_state
        state["old_stale"] = deque([10.0])
        state["fresh_old"] = deque([140.0])
        state["fresh_new"] = deque([190.0])
        application._prune_rate_limit_state(now=201.0, max_keys=2)
        self.assertNotIn("old_stale", state)
        self.assertNotIn("fresh_old", state)
        self.assertIn("fresh_new", state)
        self.assertLessEqual(len(state), 2)

    def test_rate_limiter_bounded_after_load(self):
        state = application.rate_limit_state
        total = application.RATE_LIMIT_MAX_KEYS + 2
        for i in range(total):
            application._is_request_allowed(f"bounded:{i}", 60)
        self.assertLessEqual(len(state), application.RATE_LIMIT_MAX_KEYS + 1)
        self.assertNotIn("bounded:0", state)

    def test_existing_limit_semantics_preserved(self):
        key = application._rate_limit_key("login", "203.0.113.7")
        allowed = [application.is_login_request_allowed("203.0.113.7") for _ in range(11)]
        self.assertEqual(sum(allowed), 10)
        self.assertFalse(application.is_login_request_allowed("203.0.113.7"))
        self.assertEqual(len(application.rate_limit_state[key]), 10)

    def test_concurrent_single_key_no_crash_and_limit(self):
        key = "concurrent:single"
        limit = 10
        barrier = threading.Barrier(8)
        errors = []
        results = []

        def worker():
            allowed = 0
            try:
                barrier.wait()
                for _ in range(200):
                    if application._is_request_allowed(key, limit):
                        allowed += 1
            except Exception as error:
                errors.append(error)
            results.append(allowed)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(sum(results), limit)
        self.assertEqual(len(application.rate_limit_state[key]), limit)

    def test_concurrent_distinct_keys_no_crash(self):
        barrier = threading.Barrier(8)
        errors = []

        def worker(idx):
            try:
                barrier.wait()
                for i in range(50):
                    application._is_request_allowed(f"concurrent:distinct:w{idx}:k{i}", 60)
            except Exception as error:
                errors.append(error)

        threads = [threading.Thread(target=worker, args=(w,)) for w in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(application.rate_limit_state), 8 * 50)

    def test_large_body_returns_413(self):
        application.rate_limit_state.clear()
        client = application.app.test_client()
        huge = json.dumps({"message": "x" * 700_000})
        response = client.post(
            "/chat",
            data=huge,
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 413)
        payload = response.get_json()
        self.assertFalse(payload["success"])
        self.assertEqual(payload["code"], "PAYLOAD_TOO_LARGE")
        self.assertEqual(payload["error"], "Solicitud demasiado grande.")
        self.assertFalse(application.rate_limit_state)


if __name__ == "__main__":
    unittest.main()