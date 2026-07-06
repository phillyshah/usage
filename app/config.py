"""Environment-backed settings (pydantic-settings).

All runtime configuration lives here. Secrets come from the environment / .env
and are never committed. See .env.example for the full list.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- Anthropic (vision fallback) ---
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"

    # --- Supabase (Postgres + Storage) ---
    supabase_url: str = ""
    supabase_service_key: str = ""

    # --- Behaviour knobs ---
    retention_days: int = 14
    sum_tolerance: float = 0.01
    # Minimum model confidence to write a value: one of high|medium|low.
    vision_conf_threshold: str = "medium"

    # --- Local / offline development ---
    # When true the app runs without Supabase or Anthropic: it uses an on-disk
    # store that mimics the Storage buckets and the deterministic-only pipeline.
    # Lets the UI and the fixtures run with no live credentials.
    offline_mode: bool = False
    local_data_dir: str = ".localdata"

    @property
    def has_supabase(self) -> bool:
        return bool(self.supabase_url and self.supabase_service_key) and not self.offline_mode

    @property
    def has_anthropic(self) -> bool:
        return bool(self.anthropic_api_key) and not self.offline_mode


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
