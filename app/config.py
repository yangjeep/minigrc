"""Application configuration, sourced from environment variables."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GRC_", env_file=".env", extra="ignore")

    data_dir: str = "./data"
    database_path: str | None = None
    log_level: str = "INFO"
    app_env: str = "development"

    max_upload_mb: int = 25
    max_vendor_roster_rows: int = 5000
    session_ttl_hours: int = 24 * 7
    session_cookie_secure: bool = False

    # Public base URL this app is served at (e.g. "https://grc.example.com"),
    # used only to derive OAuth redirect URIs safely — never trust the
    # request's Host header for this. Required for Google OIDC login.
    public_base_url: str = ""

    google_oidc_client_id: str = ""
    google_oidc_client_secret: str = ""
    google_oidc_allowed_domains: str = ""

    @property
    def resolved_database_path(self) -> str:
        return self.database_path or f"{self.data_dir.rstrip('/')}/grc.db"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def google_oidc_allowed_domains_set(self) -> set[str]:
        return {d.strip().lower() for d in self.google_oidc_allowed_domains.split(",") if d.strip()}

    @property
    def google_oidc_enabled(self) -> bool:
        return bool(self.google_oidc_client_id and self.google_oidc_client_secret and self.public_base_url)

    @property
    def google_oidc_redirect_uri(self) -> str:
        return f"{self.public_base_url.rstrip('/')}/auth/google/callback"


@lru_cache
def get_settings() -> Settings:
    return Settings()
