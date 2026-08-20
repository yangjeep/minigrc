"""Admin route tests for issue #15's BYOK AI provider settings:
/admin/ai/settings.
"""

from __future__ import annotations

from cryptography.fernet import Fernet
from sqlalchemy import select

from app.models import AiProviderSettings, AuditEvent
from tests.conftest import extract_csrf_token

TEST_KEY = Fernet.generate_key().decode()


def test_settings_page_requires_admin(logged_in_client):
    response = logged_in_client.get("/admin/ai/settings")
    assert response.status_code == 403


def test_settings_page_loads_for_admin(admin_client):
    response = admin_client.get("/admin/ai/settings")
    assert response.status_code == 200
    assert "Not configured" in response.text


def test_admin_can_save_valid_configuration(admin_client, app):
    app.state.settings.encryption_key = TEST_KEY
    page = admin_client.get("/admin/ai/settings")
    csrf_token = extract_csrf_token(page.text)

    response = admin_client.post(
        "/admin/ai/settings",
        data={
            "enabled": "on",
            "display_name": "Test Provider",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o-mini",
            "api_key": "sk-test-key-value",
            "timeout_seconds": "30",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" not in response.headers["location"]

    with app.state.session_factory() as session:
        row = session.scalar(select(AiProviderSettings))
        assert row is not None
        assert row.enabled is True
        assert row.base_url == "https://api.openai.com/v1"
        assert row.secret_id is not None
        events = session.scalars(select(AuditEvent).where(AuditEvent.entity_type == "ai_provider_settings"))
        assert any(events)

    # The plaintext key is never redisplayed after save.
    settings_page = admin_client.get("/admin/ai/settings")
    assert "sk-test-key-value" not in settings_page.text


def test_admin_save_rejects_ssrf_risky_base_url(admin_client, app):
    page = admin_client.get("/admin/ai/settings")
    csrf_token = extract_csrf_token(page.text)

    response = admin_client.post(
        "/admin/ai/settings",
        data={
            "enabled": "on",
            "display_name": "Malicious",
            "base_url": "http://169.254.169.254/latest/meta-data",
            "model": "gpt-4o-mini",
            "api_key": "",
            "timeout_seconds": "30",
            "csrf_token": csrf_token,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]

    with app.state.session_factory() as session:
        assert session.scalar(select(AiProviderSettings)) is None  # never persisted


def test_timeout_seconds_is_capped_server_side(admin_client, app):
    """The form's max="120" is a client-side hint only — a bypassed/
    scripted submission must still be capped, so an admin (or a
    compromised admin session) can't configure an effectively-unbounded
    provider timeout."""
    page = admin_client.get("/admin/ai/settings")
    csrf_token = extract_csrf_token(page.text)
    admin_client.post(
        "/admin/ai/settings",
        data={
            "enabled": "on",
            "display_name": "Test",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o-mini",
            "api_key": "",
            "timeout_seconds": "999999",
            "csrf_token": csrf_token,
        },
    )
    with app.state.session_factory() as session:
        row = session.scalar(select(AiProviderSettings))
        assert row.timeout_seconds == 120


def test_blank_api_key_resubmission_keeps_existing_secret(admin_client, app):
    app.state.settings.encryption_key = TEST_KEY
    page = admin_client.get("/admin/ai/settings")
    csrf_token = extract_csrf_token(page.text)
    admin_client.post(
        "/admin/ai/settings",
        data={
            "enabled": "on",
            "display_name": "Test Provider",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o-mini",
            "api_key": "sk-original-key",
            "timeout_seconds": "30",
            "csrf_token": csrf_token,
        },
    )
    with app.state.session_factory() as session:
        original_secret_id = session.scalar(select(AiProviderSettings)).secret_id

    page = admin_client.get("/admin/ai/settings")
    csrf_token = extract_csrf_token(page.text)
    admin_client.post(
        "/admin/ai/settings",
        data={
            "enabled": "on",
            "display_name": "Test Provider Renamed",
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o-mini",
            "api_key": "",
            "timeout_seconds": "30",
            "csrf_token": csrf_token,
        },
    )
    with app.state.session_factory() as session:
        row = session.scalar(select(AiProviderSettings))
        assert row.secret_id == original_secret_id
        assert row.display_name == "Test Provider Renamed"
