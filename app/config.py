"""Application configuration, sourced from environment variables."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GRC_", env_file=".env", extra="ignore")

    data_dir: str = "./data"
    database_path: str | None = None
    # Standard unprefixed DATABASE_URL (not GRC_DATABASE_URL) — matches the
    # convention most Postgres-hosting platforms use. When set, this takes
    # priority over database_path/data_dir (see resolved_database_path).
    database_url: str = Field(default="", validation_alias="DATABASE_URL")
    log_level: str = "INFO"
    app_env: str = "development"

    max_upload_mb: int = 25
    max_vendor_roster_rows: int = 5000
    session_ttl_hours: int = 24 * 7
    session_cookie_secure: bool = False

    # Optional watched import directory (Feature 9). The worker only
    # watches a directory when both are set — no default path, since a
    # default would silently start watching an arbitrary location.
    import_watch_dir: str = ""
    import_watch_importer: str = ""

    # Public base URL this app is served at (e.g. "https://grc.example.com"),
    # used only to derive OAuth redirect URIs safely — never trust the
    # request's Host header for this. Required for Google OIDC login.
    public_base_url: str = ""

    google_oidc_client_id: str = ""
    google_oidc_client_secret: str = ""
    google_oidc_allowed_domains: str = ""

    # OAuth for the org-level Drive connection stays distinct from OIDC
    # login above, even if an operator points both at the same Google
    # Cloud project's client credentials.
    google_drive_client_id: str = ""
    google_drive_client_secret: str = ""

    # Optional: request the read-only Workspace Directory scope in the
    # same Drive connection consent grant. See
    # app/google_workspace_directory.py.
    google_workspace_directory_enabled: bool = False

    encryption_key: str = ""

    @property
    def resolved_database_path(self) -> str:
        return self.database_path or f"{self.data_dir.rstrip('/')}/grc.db"

    @property
    def resolved_engine_target(self) -> str:
        """What to pass to app.db.build_engine — DATABASE_URL if set, else the SQLite path."""
        return self.database_url or self.resolved_database_path

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

    @property
    def google_drive_configured(self) -> bool:
        return bool(
            self.google_drive_client_id
            and self.google_drive_client_secret
            and self.public_base_url
            and self.encryption_key
        )

    @property
    def google_drive_redirect_uri(self) -> str:
        return f"{self.public_base_url.rstrip('/')}/connectors/google-drive/callback"


@lru_cache
def get_settings() -> Settings:
    return Settings()
