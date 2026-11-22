from contextlib import ExitStack
from unittest import mock

from services.ai_router import router

TEST_ONLY_API_KEY = "test-only-not-a-real-api-key"


def provider_record(provider, provider_id, priority=1, active=True, model=None):
    return {
        "id": provider_id,
        "provider": provider,
        "priority": priority,
        "active": active,
        "model": model,
    }


def gemini_provider_record(provider_id=1, priority=1, model=None):
    return provider_record("gemini", provider_id, priority=priority, model=model)


class SimulatedAIProviders:
    def __init__(self, *records, api_key=TEST_ONLY_API_KEY):
        self.records = list(records)
        self.api_key = api_key
        self._stack = None

    def start(self):
        if self._stack is not None:
            return self
        router.clear_cache()
        stack = ExitStack()
        stack.callback(router.clear_cache)
        stack.enter_context(
            mock.patch("services.ai_router.list_ai_providers", return_value=list(self.records))
        )
        stack.enter_context(
            mock.patch("services.ai_router.get_ai_provider_by_name", side_effect=self._get_by_name)
        )
        stack.enter_context(
            mock.patch("services.ai_router.encryption_is_configured", return_value=True)
        )
        stack.enter_context(
            mock.patch("services.ai_router.get_decrypted_api_key", return_value=self.api_key)
        )
        self._stack = stack
        return self

    def stop(self):
        if self._stack is None:
            return
        self._stack.close()
        self._stack = None

    def _get_by_name(self, name):
        return next((dict(record) for record in self.records if record["provider"] == name), None)

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc_value, traceback):
        self.stop()


def simulated_gemini_provider(model=None):
    return SimulatedAIProviders(gemini_provider_record(model=model))
