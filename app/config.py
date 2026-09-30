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

    # --- Which service reads the tickets ---
    # "anthropic" | "openrouter". OpenRouter is primary: the open-weight model
    # reads these tickets well at roughly a tenth of the cost, and a spend cap
    # on the other account is what stopped a day's work once already.
    vision_provider: str = "openrouter"

    # The stronger second reader, used ONLY on tickets the first one left with
    # a genuine gap — see VISION_ESCALATE_FIELDS. Cost therefore scales with
    # the failure rate rather than with volume: at ~10% of tickets this is
    # cheaper than reading everything with Claude AND more accurate than
    # reading everything with the open-weight model.
    #
    # Empty or "none" turns it off.
    vision_escalate_model: str = "claude-opus-5-5"
    # Which empty cells are worth paying to re-read. A blank Lot or Expiry is
    # recoverable from the barcode and from the Expiry Log; these four are not
    # recoverable from anywhere, and they are what the invoice is built from.
    vision_escalate_fields: str = "unit_price,surgeon,hospital,surgery_date"

    # --- OpenRouter (open-weight models) ---
    openrouter_api_key: str = ""
    # A DEFAULT, NOT A GUESS. OpenRouter's catalogue is namespaced
    # (vendor/model) and it moves — models are added, renamed and retired, and
    # which of them accept IMAGES varies. This one was chosen for this job:
    # open-weight, vision-capable, and strong enough at reasoning to do the
    # part that is actually hard here — matching prices to labels and
    # reconciling to the grand total, not character recognition.
    openrouter_model: str = "qwen/qwen3-vl-235b-a22b-instruct"
    # The backup OpenRouter walks to when the first cannot serve the request.
    # It MUST also accept images; the text-only fallback the dashboard uses
    # would fail every ticket here. Set to "none" to send no fallback.
    openrouter_fallback_model: str = "qwen/qwen3-vl-32b-instruct"

    # --- Anthropic (vision fallback) ---
    anthropic_api_key: str = ""
    # The current Sonnet. NOTE: the VPS .env sets ANTHROPIC_MODEL, and that
    # pin wins over this default — changing it here alone moves nothing.
    anthropic_model: str = "claude-sonnet-5-5"

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

    # --- Patient initials ---
    # Whether the two initials are KEPT. It no longer decides whether they are
    # read: the ticket image is sent whole, patient sticker included, so the
    # model sees it either way. Off means assemble discards the two letters and
    # nothing about the patient reaches the database or the workbook.
    #
    # The ticket image is no longer masked before it is sent or stored, so a
    # HIPAA BAA on the Anthropic account is required for the extraction call
    # itself, not just for this flag.
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

    @property
    def has_vision(self) -> bool:
        """Is SOME reader configured? Provider-agnostic on purpose: every
        caller that used to ask has_anthropic really meant this."""
        if self.offline_mode:
            return False
        if (self.vision_provider or "").strip().lower() == "openrouter":
            return bool(self.openrouter_api_key)
        return bool(self.anthropic_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
