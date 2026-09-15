import unittest
from unittest import mock

import app as application


class ChatPhoneRateLimitingTests(unittest.TestCase):
    def setUp(self):
        application.rate_limit_state.clear()

    def test_phone_allowed_under_limit(self):
        self.assertTrue(application.is_chat_phone_request_allowed("+549 11 1234-5678", 1))

    def test_phone_rejected_over_limit(self):
        limit = application.CHAT_PHONE_REQUEST_LIMIT
        for _ in range(limit):
            self.assertTrue(application.is_chat_phone_request_allowed("+5491112345678", 1))
        self.assertFalse(application.is_chat_phone_request_allowed("+5491112345678", 1))

    def test_different_phones_have_independent_counters(self):
        limit = application.CHAT_PHONE_REQUEST_LIMIT
        for _ in range(limit):
            self.assertTrue(application.is_chat_phone_request_allowed("+5491112345678", 1))
        self.assertTrue(application.is_chat_phone_request_allowed("+5491112345679", 1))

    def test_equivalent_formats_share_counter(self):
        limit = application.CHAT_PHONE_REQUEST_LIMIT
        for _ in range(limit):
            self.assertTrue(application.is_chat_phone_request_allowed("+549 11 1234-5678", 1))
        self.assertFalse(application.is_chat_phone_request_allowed("5491112345678", 1))

    def test_missing_phone_does_not_apply_phone_rate_limit(self):
        self.assertTrue(application.is_chat_phone_request_allowed("", 1))
        self.assertTrue(application.is_chat_phone_request_allowed(None, 1))

    def test_existing_ip_rate_limit_still_works(self):
        self.assertTrue(application.is_chat_request_allowed("1.2.3.4", 1))
        limit = application.CHAT_REQUEST_LIMIT
        for _ in range(limit):
            application.is_chat_request_allowed("1.2.3.4", 1)
        self.assertFalse(application.is_chat_request_allowed("1.2.3.4", 1))

    def test_phone_not_exposed_in_logs(self):
        with mock.patch("app.logger") as mock_logger:
            limit = application.CHAT_PHONE_REQUEST_LIMIT
            for _ in range(limit):
                application.is_chat_phone_request_allowed("+549 11 1234-5678", 1)
            logged_messages = " ".join(str(call) for call in mock_logger.method_calls)
            self.assertNotIn("5491112345678", logged_messages)


if __name__ == "__main__":
    unittest.main()
