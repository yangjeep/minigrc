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
    # convention most Postgres-hosting platforms use. `resolved_engine_target`
    # prefers this when set, but `reject_ambiguous_database_config` below
    # treats both being set at once as a startup error rather than silently
    # picking one — see that function for why the check isn't a validator
    # here.
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

    # S3-compatible evidence artifact repository (issue #32) — plain,
    # operator-set deployment configuration (same category as
    # DATABASE_URL), not a runtime-configurable/DB-stored Connection row
    # like the AWS/Drive connectors. `s3_endpoint_url` is trusted
    # deployment config, not per-request attacker input — see
    # docs/superpowers/specs/2026-08-19-issue32-evidence-repository-design.md
    # §5 for the SSRF-boundary reasoning.
    s3_endpoint_url: str = ""
    s3_bucket: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_region: str = "us-east-1"

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

    @property
    def evidence_repository_configured(self) -> bool:
        return bool(
            self.s3_endpoint_url and self.s3_bucket and self.s3_access_key_id and self.s3_secret_access_key
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reject_ambiguous_database_config(settings: Settings) -> None:
    """Raise if `settings` selects more than one relational backend (issue #22).

    This is a plain function called explicitly at the point
    `resolved_engine_target` is actually used (`app.main.create_app`), not a
    pydantic validator on `Settings` construction. `get_settings()` builds a
    real `Settings()` from the ambient environment on every call site,
    including inside `create_app` *before* its test-only `database_path`
    override (which always clears `database_url`) is applied — validating
    at construction time would reject an ambient environment that has both
    variables set even when the caller is about to override one of them.
    Checking only the final, actually-used settings avoids that false
    positive while still closing the real gap: an operator who sets both
    `DATABASE_URL` and `GRC_DATABASE_PATH` for a real deployment now fails
    fast with a clear message instead of `resolved_engine_target` silently
    preferring `DATABASE_URL`.

    A plain `RuntimeError` also avoids pydantic's `ValidationError`
    formatting, which embeds a (partially truncated) repr of the input
    dict — for `Settings` that would risk echoing tail characters of
    `encryption_key` into a startup error message/log.
    """
    if settings.database_url and settings.database_path:
        raise RuntimeError(
            "Ambiguous database configuration: both DATABASE_URL and "
            "GRC_DATABASE_PATH are set. A miniGRC deployment must select "
            "exactly one relational backend — unset one of them."
        )
