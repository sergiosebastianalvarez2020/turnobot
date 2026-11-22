"""
tests/test_ai_config.py — Pruebas del servicio de configuración de proveedores de IA.

Requiere SQLite temporal (fixture sqlite_temp_db) y AI_ENCRYPTION_KEY.
"""

import pytest

from services.ai_config import (
    AIConfigError,
    create_ai_provider,
    delete_ai_provider,
    get_ai_provider,
    get_ai_provider_by_name,
    get_decrypted_api_key,
    list_ai_providers,
    set_ai_provider_active,
    set_ai_provider_priority,
    update_ai_provider,
)

pytestmark = pytest.mark.no_pg


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch):
    monkeypatch.setenv("AI_ENCRYPTION_KEY", "x" * 32)


def _create(provider="gemini", model="gemini-2.5-flash", priority=1, active=True):
    return create_ai_provider(
        provider=provider,
        api_key="sk-test-key-" + provider,
        model=model,
        priority=priority,
        active=active,
    )


def test_create_and_list_provider(sqlite_temp_db):
    pid = _create("gemini", "gemini-2.5-flash", 1, True)
    providers = list_ai_providers()
    assert len(providers) == 1
    p = providers[0]
    assert p["id"] == pid
    assert p["provider"] == "gemini"
    assert p["model"] == "gemini-2.5-flash"
    assert p["priority"] == 1
    assert p["active"] is True
    assert p["has_credentials"] is True


def test_create_duplicate_provider_raises(sqlite_temp_db):
    _create("gemini")
    with pytest.raises(AIConfigError, match="ya está configurado"):
        _create("gemini")


def test_get_ai_provider_by_name(sqlite_temp_db):
    pid = _create("groq", "llama-3.1-8b", 2, False)
    p = get_ai_provider_by_name("groq")
    assert p is not None
    assert p["id"] == pid
    assert p["model"] == "llama-3.1-8b"
    assert p["active"] is False


def test_get_ai_provider_not_found(sqlite_temp_db):
    assert get_ai_provider(99999) is None


def test_update_provider_preserves_key(sqlite_temp_db):
    pid = _create("gemini", "gemini-2.5-flash", 1, True)
    original_key = get_decrypted_api_key(pid)
    assert original_key == "sk-test-key-gemini"

    ok = update_ai_provider(pid, model="gemini-2.5-pro", priority=5, active=False)
    assert ok is True

    # La clave debe mantenerse
    assert get_decrypted_api_key(pid) == original_key
    p = get_ai_provider(pid)
    assert p["model"] == "gemini-2.5-pro"
    assert p["priority"] == 5
    assert p["active"] is False


def test_update_provider_rotates_key(sqlite_temp_db):
    pid = _create("gemini")
    update_ai_provider(pid, api_key="sk-new-key-rotated")
    assert get_decrypted_api_key(pid) == "sk-new-key-rotated"


def test_update_provider_no_changes_returns_false(sqlite_temp_db):
    pid = _create("gemini")
    ok = update_ai_provider(pid)
    assert ok is False


def test_activate_deactivate_provider(sqlite_temp_db):
    pid = _create("gemini", active=True)
    assert get_ai_provider(pid)["active"] is True

    ok = set_ai_provider_active(pid, False)
    assert ok is True
    assert get_ai_provider(pid)["active"] is False

    ok = set_ai_provider_active(pid, True)
    assert ok is True
    assert get_ai_provider(pid)["active"] is True


def test_set_ai_provider_active_not_found(sqlite_temp_db):
    assert set_ai_provider_active(99999, True) is False


def test_change_priority(sqlite_temp_db):
    pid = _create("gemini", priority=1)
    ok = set_ai_provider_priority(pid, 10)
    assert ok is True
    assert get_ai_provider(pid)["priority"] == 10


def test_change_priority_not_found(sqlite_temp_db):
    assert set_ai_provider_priority(99999, 5) is False


def test_delete_provider(sqlite_temp_db):
    pid = _create("gemini")
    ok = delete_ai_provider(pid)
    assert ok is True
    assert get_ai_provider(pid) is None
    assert len(list_ai_providers()) == 0


def test_delete_provider_not_found(sqlite_temp_db):
    assert delete_ai_provider(99999) is False


def test_list_ordered_by_priority(sqlite_temp_db):
    _create("cerebras", priority=10)
    _create("groq", priority=1)
    _create("openrouter", priority=5)
    providers = list_ai_providers()
    assert [p["provider"] for p in providers] == ["groq", "openrouter", "cerebras"]


def test_create_without_encryption_key_raises(sqlite_temp_db, monkeypatch):
    monkeypatch.delenv("AI_ENCRYPTION_KEY", raising=False)
    with pytest.raises(AIConfigError, match="no está configurada"):
        _create("gemini")


def test_create_with_empty_api_key_raises(sqlite_temp_db):
    with pytest.raises(AIConfigError, match="API key no puede estar vacía"):
        create_ai_provider(
            provider="groq", api_key="   ", model="llama-3.1-8b-instant", priority=1, active=True
        )


def test_create_duplicate_priority_raises(sqlite_temp_db):
    _create("gemini", priority=1)
    with pytest.raises(AIConfigError, match="ya está en uso"):
        _create("groq", priority=1)


def test_update_priority_duplicate_raises(sqlite_temp_db):
    _create("gemini", priority=1)
    _create("groq", priority=2)
    pid = get_ai_provider_by_name("groq")["id"]
    with pytest.raises(AIConfigError, match="ya está en uso"):
        set_ai_provider_priority(pid, 1)


def test_update_priority_same_value_ok(sqlite_temp_db):
    pid = _create("gemini", priority=1)
    ok = set_ai_provider_priority(pid, 1)
    assert ok is True


def test_provider_has_encryption_key_version(sqlite_temp_db):
    pid = _create("gemini", priority=1)
    p = get_ai_provider(pid)
    assert p["encryption_key_version"] == 1


def test_update_api_key_bumps_key_version(sqlite_temp_db):
    pid = _create("gemini", priority=1)
    original_version = get_ai_provider(pid)["encryption_key_version"]
    update_ai_provider(pid, api_key="sk-new-key-rotated")
    new_version = get_ai_provider(pid)["encryption_key_version"]
    assert new_version == original_version
    # La clave descifrada debe ser la nueva
    assert get_decrypted_api_key(pid) == "sk-new-key-rotated"
