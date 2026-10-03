"""Application configuration loaded from environment variables via pydantic-settings."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PLACEHOLDER_MARKERS = ("change-me", "your-", "xxxxxxxx")
ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def is_placeholder(value: str | None) -> bool:
    """True when a secret is empty or still one of the documented placeholder values."""
    text = (value or "").strip().lower()
    return not text or any(marker in text for marker in PLACEHOLDER_MARKERS)


class Settings(BaseSettings):
    """All configuration loaded from .env with validation and defaults."""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- App ---
    ENV: Literal["development", "production"] = "development"
    SECRET_KEY: str = "change-me-long-random-string"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    # Opt-in: create the synthetic demo accounts (password DemoPass123) on startup, even in
    # production. Only for a public portfolio demo that holds no real data.
    DEMO_DATA: bool = False
    CLINIC_TIMEZONE: str = "Asia/Kolkata"  # doctors' weekly hours are in this time zone

    # --- Database ---
    DATABASE_URL: str

    # --- Admin bootstrap ---
    ADMIN_BOOTSTRAP_EMAIL: str = "admin@example.com"
    ADMIN_BOOTSTRAP_PASSWORD: str = "change-me-strong-password"

    # --- AWS S3 ---
    STORAGE_BACKEND: Literal["local", "s3"] = "local"
    LOCAL_STORAGE_DIR: str = ""  # default: <project>/.local_storage (git-ignored)
    AWS_ACCESS_KEY_ID: str = ""
    AWS_SECRET_ACCESS_KEY: str = ""
    AWS_REGION: str = "ap-south-1"
    S3_BUCKET_NAME: str = "clinicalscribe-private-files"
    PRESIGNED_URL_EXPIRY_SECONDS: int = 300

    # --- LLM ---
    # "mock" is an explicit, development-only opt-in that returns canned, transcript-independent
    # output. It is never selected implicitly when a key is missing.
    LLM_PROVIDER: Literal["gemini", "mock"] = "gemini"
    LLM_API_KEY: str = ""
    LLM_MODEL: str = "gemini-3.8-flash"
    LLM_BASE_URL: str = "https://generativelanguage.googleapis.com"

    # --- ElevenLabs ---
    ELEVENLABS_API_KEY: str = ""

    # Speech-to-text
    STT_PROVIDER: Literal["elevenlabs", "mock"] = "mock"
    STT_MODEL: str = "scribe_v2"
    STT_LANGUAGE: str = "en"
    MAX_AUDIO_MB: int = 25
    DELETE_AUDIO_AFTER_TRANSCRIPTION: bool = False

    # Voice agent / TTS
    VOICE_AGENT_PROVIDER: Literal["elevenlabs", "none"] = "none"
    ELEVENLABS_AGENT_ID: str = ""
    ELEVENLABS_VOICE_ID: str = ""

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def normalize_database_url(cls, v: str) -> str:
        """Normalize postgres:// or postgresql:// to postgresql+psycopg://."""
        v = re.sub(r"^postgres://", "postgresql+psycopg://", v)
        v = re.sub(r"^postgresql://", "postgresql+psycopg://", v)
        # Don't double-transform if already correct
        v = v.replace("postgresql+psycopg+psycopg://", "postgresql+psycopg://")
        return v

    @field_validator("CLINIC_TIMEZONE")
    @classmethod
    def validate_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"CLINIC_TIMEZONE {v!r} is not a known IANA time zone")
        return v

    @field_validator("SECRET_KEY", mode="after")
    @classmethod
    def validate_secret_key(cls, v: str) -> str:
        if len(v) < 16:
            raise ValueError("SECRET_KEY must be at least 16 characters")
        return v

    @property
    def llm_configured(self) -> bool:
        """True when a real LLM call can be made."""
        return self.LLM_PROVIDER == "gemini" and not is_placeholder(self.LLM_API_KEY)

    @property
    def is_production(self) -> bool:
        return self.ENV == "production"

    @property
    def cookie_secure(self) -> bool:
        return self.is_production

    @property
    def max_audio_bytes(self) -> int:
        return self.MAX_AUDIO_MB * 1024 * 1024

    @property
    def database_requires_ssl(self) -> bool:
        """Require SSL for non-localhost database hosts."""
        parsed = urlsplit(self.DATABASE_URL)
        if parsed.scheme.startswith("sqlite"):
            return False
        return parsed.hostname not in {None, "localhost", "127.0.0.1", "::1"}


def validate_production(settings: Settings) -> None:
    """Fail fast at startup when a production deployment is misconfigured."""
    if settings.STORAGE_BACKEND != "s3":
        raise ValueError("FATAL: STORAGE_BACKEND must be 's3' in production")
    if settings.LLM_PROVIDER == "mock":
        raise ValueError("FATAL: LLM_PROVIDER=mock is not allowed in production")
    if settings.STT_PROVIDER == "mock":
        raise ValueError("FATAL: STT_PROVIDER=mock is not allowed in production")
    critical = {
        "SECRET_KEY": settings.SECRET_KEY,
        "AWS_ACCESS_KEY_ID": settings.AWS_ACCESS_KEY_ID,
        "AWS_SECRET_ACCESS_KEY": settings.AWS_SECRET_ACCESS_KEY,
        "LLM_API_KEY": settings.LLM_API_KEY,
        "LLM_MODEL": settings.LLM_MODEL,
        "ADMIN_BOOTSTRAP_PASSWORD": settings.ADMIN_BOOTSTRAP_PASSWORD,
    }
    if settings.STT_PROVIDER == "elevenlabs" or settings.VOICE_AGENT_PROVIDER == "elevenlabs":
        critical["ELEVENLABS_API_KEY"] = settings.ELEVENLABS_API_KEY
    if settings.VOICE_AGENT_PROVIDER == "elevenlabs":
        critical["ELEVENLABS_VOICE_ID"] = settings.ELEVENLABS_VOICE_ID
    for name, value in critical.items():
        invalid = is_placeholder(value)
        if name == "AWS_ACCESS_KEY_ID" and re.fullmatch(r"AKIAX+", value):
            invalid = True
        if invalid:
            raise ValueError(
                f"FATAL: {name} is missing or appears to be a placeholder. "
                "Set a real value in production."
            )
    if len(settings.SECRET_KEY) < 32:
        raise ValueError("FATAL: SECRET_KEY must be at least 32 characters in production")
    if settings.database_requires_ssl and "sslmode=require" not in settings.DATABASE_URL:
        raise ValueError("FATAL: DATABASE_URL must set sslmode=require in production")


@lru_cache
def get_settings() -> Settings:
    """Cached singleton settings instance (validated strictly when ENV=production)."""
    settings = Settings()
    if settings.is_production:
        validate_production(settings)
    return settings
