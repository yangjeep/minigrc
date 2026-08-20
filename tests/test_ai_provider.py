"""Unit tests for issue #15's BYOK provider config resolution and SSRF
guard (app/ai_provider.py)."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.ai_provider import InvalidProviderUrlError, resolve_ai_provider_config, validate_provider_base_url
from app.models import AiProviderSettings
from app.secrets import create_encrypted_secret

TEST_KEY = Fernet.generate_key().decode()


class TestValidateProviderBaseUrl:
    @pytest.mark.parametrize(
        "base_url",
        [
            "https://api.openai.com/v1",
            "http://localhost:11434/v1",  # a common self-hosted Ollama-style gateway
            "http://192.168.1.50:8000/v1",  # a self-hosted gateway on a private network
            "https://my-llm-gateway.internal.example.com/v1",
        ],
    )
    def test_accepts_legitimate_endpoints(self, base_url):
        validate_provider_base_url(base_url)  # must not raise

    @pytest.mark.parametrize(
        "base_url,expected_message_fragment",
        [
            ("ftp://example.com/v1", "http or https"),
            ("not-a-url", "http or https"),
            ("https://", "host"),
            ("https://user:pass@example.com/v1", "credentials"),
            ("http://169.254.169.254/latest/meta-data", "metadata"),
            ("http://169.254.1.1/", "metadata"),
            ("http://metadata.google.internal/computeMetadata/v1/", "metadata"),
            ("http://[fd00:ec2::254]/latest/meta-data", "metadata"),
        ],
    )
    def test_rejects_dangerous_or_malformed_endpoints(self, base_url, expected_message_fragment):
        with pytest.raises(InvalidProviderUrlError, match=expected_message_fragment):
            validate_provider_base_url(base_url)


class TestResolveAiProviderConfig:
    def test_no_row_is_unusable(self, app):
        with app.state.session_factory() as session:
            resolved = resolve_ai_provider_config(session, encryption_key=TEST_KEY)
        assert resolved.usable is False
        assert resolved.source == "unconfigured"

    def test_disabled_row_is_unusable(self, app):
        with app.state.session_factory() as session:
            session.add(
                AiProviderSettings(
                    enabled=False,
                    display_name="Test",
                    base_url="https://api.openai.com/v1",
                    model="gpt-4o-mini",
                    updated_by="admin@example.com",
                )
            )
            session.commit()
            resolved = resolve_ai_provider_config(session, encryption_key=TEST_KEY)
        assert resolved.usable is False

    def test_enabled_row_with_valid_secret_is_usable(self, app):
        with app.state.session_factory() as session:
            secret = create_encrypted_secret(
                session, name="test-ai-key", plaintext="sk-real-key-value", actor="admin", key=TEST_KEY
            )
            session.add(
                AiProviderSettings(
                    enabled=True,
                    display_name="Test Provider",
                    base_url="https://api.openai.com/v1",
                    model="gpt-4o-mini",
                    secret_id=secret.id,
                    updated_by="admin@example.com",
                )
            )
            session.commit()
            resolved = resolve_ai_provider_config(session, encryption_key=TEST_KEY)
        assert resolved.usable is True
        assert resolved.api_key == "sk-real-key-value"
        assert resolved.source == "admin"

    def test_wrong_encryption_key_degrades_to_unusable_not_raise(self, app):
        with app.state.session_factory() as session:
            secret = create_encrypted_secret(
                session, name="test-ai-key-2", plaintext="sk-real-key-value", actor="admin", key=TEST_KEY
            )
            session.add(
                AiProviderSettings(
                    enabled=True,
                    display_name="Test",
                    base_url="https://api.openai.com/v1",
                    model="gpt-4o-mini",
                    secret_id=secret.id,
                    updated_by="admin@example.com",
                )
            )
            session.commit()
            wrong_key = Fernet.generate_key().decode()
            resolved = resolve_ai_provider_config(session, encryption_key=wrong_key)
        assert resolved.usable is False
        assert resolved.api_key == ""

    def test_ssrf_risky_base_url_degrades_to_unusable_not_raise(self, app):
        with app.state.session_factory() as session:
            session.add(
                AiProviderSettings(
                    enabled=True,
                    display_name="Test",
                    base_url="http://169.254.169.254/latest/meta-data",
                    model="gpt-4o-mini",
                    updated_by="admin@example.com",
                )
            )
            session.commit()
            resolved = resolve_ai_provider_config(session, encryption_key=TEST_KEY)
        assert resolved.usable is False

    def test_enabled_row_with_no_secret_at_all_is_still_usable(self, app):
        """A self-hosted gateway that needs no API key is a valid,
        deliberate configuration (secret_id is None) — distinct from a
        secret that was configured but fails to resolve (regression test
        below), which must NOT look the same."""
        with app.state.session_factory() as session:
            session.add(
                AiProviderSettings(
                    enabled=True,
                    display_name="Local gateway",
                    base_url="http://localhost:11434/v1",
                    model="llama3",
                    secret_id=None,
                    updated_by="admin@example.com",
                )
            )
            session.commit()
            resolved = resolve_ai_provider_config(session, encryption_key=TEST_KEY)
        assert resolved.usable is True
        assert resolved.api_key == ""

    def test_missing_model_is_unusable(self, app):
        with app.state.session_factory() as session:
            session.add(
                AiProviderSettings(
                    enabled=True,
                    display_name="Test",
                    base_url="https://api.openai.com/v1",
                    model="",
                    updated_by="admin@example.com",
                )
            )
            session.commit()
            resolved = resolve_ai_provider_config(session, encryption_key=TEST_KEY)
        assert resolved.usable is False
