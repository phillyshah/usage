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
    # A single line price at/above this is implausible for these tickets and is
    # flagged as a likely misread digit (e.g. "$650" read as 8650). Real line
    # prices run $50–$2,500, so 7500 leaves generous headroom.
    price_sanity_max: float = 7500

    # --- Patient initials (PHI) ---
    # OFF by default: with this false the app never reads patient data at all,
    # which is the original LABEL_EXTRACTION_BUILD_SPEC §9 posture. Turning it on
    # sends ONLY the cropped patient sticker to the vision API and stores ONLY
    # the two initials — see app/pipeline/patient.py. Requires a HIPAA BAA on the
    # Anthropic account.
    extract_patient_initials: bool = False

    # --- Outbound email (daily status notification) ---
    # Deliberately the same variable names as the Maxx dashboard, so one set of
    # relay credentials copies between projects without translation. "Not
    # configured" is a normal state the app ships in: email_configuration()
    # returns a *reason* rather than raising, and the Notifications card says it
    # in words instead of showing an error.
    email_provider: str = "smtp"          # smtp | resend | postmark
    email_from: str = ""                  # envelope sender the relay authenticates as
    email_api_key: str = ""               # resend / postmark only
    # The same authenticated Hostinger relay the dashboard sends through: it
    # signs SPF and DKIM and carries the provider's reputation, where a bare
    # address nobody has heard from would not. A host on its own still sends
    # nothing — EMAIL_FROM, SMTP_USER and SMTP_PASSWORD are all required too.
    smtp_host: str = "smtp.hostinger.com"
    smtp_port: int = 465                  # implicit TLS only — see app/email.py
    smtp_user: str = ""
    smtp_password: str = ""
    # 5pm in the team's own timezone, not UTC and not a fixed offset, so it
    # stays 5pm across the DST changeover.
    notify_timezone: str = "America/New_York"
    notify_hour: int = 17

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
