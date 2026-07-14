"""Application configuration, sourced from environment variables."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GRC_", env_file=".env", extra="ignore")

    database_path: str = "./data/grc.db"
    log_level: str = "INFO"
    app_env: str = "development"


@lru_cache
def get_settings() -> Settings:
    return Settings()
