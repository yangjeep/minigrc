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
    session_ttl_hours: int = 24 * 7
    session_cookie_secure: bool = False

    @property
    def resolved_database_path(self) -> str:
        return self.database_path or f"{self.data_dir.rstrip('/')}/grc.db"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
