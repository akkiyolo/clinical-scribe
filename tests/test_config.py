"""Configuration: URL normalisation, SSL rules and fail-fast production validation."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.config import Settings, is_placeholder, validate_production

ROOT = Path(__file__).resolve().parents[1]
PG = "postgresql+psycopg://user:pw@db.example.com/clinical?sslmode=require"


def make(**overrides) -> Settings:
    base = dict(
        DATABASE_URL=PG,
        ENV="production",
        SECRET_KEY="s" * 40,
        STORAGE_BACKEND="s3",
        AWS_ACCESS_KEY_ID="AKIAREALKEY1234567",
        AWS_SECRET_ACCESS_KEY="realsecretvalue" * 3,
        LLM_PROVIDER="gemini",
        LLM_API_KEY="AIzaRealKeyValue123",
        STT_PROVIDER="elevenlabs",
        ELEVENLABS_API_KEY="sk_realelevenlabskey",
        VOICE_AGENT_PROVIDER="none",
        ADMIN_BOOTSTRAP_PASSWORD="A-really-strong-pass-9",
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)


class TestDatabaseUrl:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("postgres://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
            (
                "postgresql://u:p@h/db?sslmode=require",
                "postgresql+psycopg://u:p@h/db?sslmode=require",
            ),
            ("postgresql+psycopg://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
            ("sqlite:///./x.db", "sqlite:///./x.db"),
        ],
    )
    def test_normalisation(self, raw, expected):
        assert Settings(_env_file=None, DATABASE_URL=raw).DATABASE_URL == expected

    @pytest.mark.parametrize(
        "url,needs_ssl",
        [
            ("postgresql://u:p@db.oregon-postgres.render.com/db", True),
            ("postgresql://u:p@localhost/db", False),
            ("postgresql://u:p@127.0.0.1/db", False),
            ("postgresql://u:p@[::1]/db", False),
            ("sqlite:///x.db", False),
        ],
    )
    def test_ssl_is_required_for_remote_hosts_only(self, url, needs_ssl):
        assert Settings(_env_file=None, DATABASE_URL=url).database_requires_ssl is needs_ssl


class TestProductionValidation:
    def test_a_complete_configuration_passes(self):
        validate_production(make())

    @pytest.mark.parametrize(
        "overrides,fragment",
        [
            (dict(STORAGE_BACKEND="local"), "STORAGE_BACKEND"),
            (dict(LLM_PROVIDER="mock"), "LLM_PROVIDER"),
            (dict(STT_PROVIDER="mock"), "STT_PROVIDER"),
            (dict(SECRET_KEY="change-me-long-random-string-xxxxxxxxxx"), "SECRET_KEY"),
            (dict(SECRET_KEY="short-but-ok-for-dev-16"), "SECRET_KEY"),
            (dict(AWS_ACCESS_KEY_ID="AKIAXXXXXXXXXXXXXXXX"), "AWS_ACCESS_KEY_ID"),
            (dict(AWS_ACCESS_KEY_ID=""), "AWS_ACCESS_KEY_ID"),
            (dict(AWS_SECRET_ACCESS_KEY="x" * 8 + "xxxxxxxx"), "AWS_SECRET_ACCESS_KEY"),
            (dict(LLM_API_KEY="your-llm-api-key"), "LLM_API_KEY"),
            (dict(LLM_API_KEY=""), "LLM_API_KEY"),
            (dict(ELEVENLABS_API_KEY="your-elevenlabs-api-key"), "ELEVENLABS_API_KEY"),
            (
                dict(ADMIN_BOOTSTRAP_PASSWORD="change-me-strong-password"),
                "ADMIN_BOOTSTRAP_PASSWORD",
            ),
            (dict(DATABASE_URL="postgresql://u:p@db.example.com/clinical"), "sslmode=require"),
            (
                dict(VOICE_AGENT_PROVIDER="elevenlabs", ELEVENLABS_VOICE_ID="your-voice-id"),
                "ELEVENLABS_VOICE_ID",
            ),
        ],
    )
    def test_problems_fail_fast_with_a_clear_message(self, overrides, fragment):
        with pytest.raises(ValueError, match=re.escape(fragment)):
            validate_production(make(**overrides))

    def test_the_error_never_contains_the_secret_value(self):
        with pytest.raises(ValueError) as caught:
            validate_production(make(LLM_API_KEY="your-super-secret-placeholder"))
        assert "your-super-secret-placeholder" not in str(caught.value)

    def test_elevenlabs_key_is_not_required_when_unused(self):
        validate_production(
            make(STT_PROVIDER="elevenlabs", ELEVENLABS_API_KEY="sk_real_value_here_123")
        )

    def test_secure_cookies_follow_the_environment(self):
        assert make().cookie_secure is True
        assert make(ENV="development").cookie_secure is False


class TestHelpers:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("", True),
            (None, True),
            ("  ", True),
            ("your-llm-api-key", True),
            ("CHANGE-ME-please", True),
            ("xxxxxxxxxxxx", True),
            ("sk_real_key_123", False),
            ("AIzaSyRealLooking", False),
        ],
    )
    def test_placeholder_detection(self, value, expected):
        assert is_placeholder(value) is expected

    def test_llm_is_only_configured_with_a_real_key_and_the_gemini_provider(self):
        assert make().llm_configured
        assert not make(LLM_PROVIDER="mock").llm_configured
        assert not make(LLM_API_KEY="your-llm-api-key").llm_configured


class TestEnvExample:
    SPEC_KEYS = {
        "ENV",
        "SECRET_KEY",
        "ACCESS_TOKEN_EXPIRE_MINUTES",
        "DATABASE_URL",
        "ADMIN_BOOTSTRAP_EMAIL",
        "ADMIN_BOOTSTRAP_PASSWORD",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_REGION",
        "S3_BUCKET_NAME",
        "PRESIGNED_URL_EXPIRY_SECONDS",
        "LLM_API_KEY",
        "LLM_MODEL",
        "ELEVENLABS_API_KEY",
        "STT_PROVIDER",
        "STT_MODEL",
        "STT_LANGUAGE",
        "MAX_AUDIO_MB",
        "DELETE_AUDIO_AFTER_TRANSCRIPTION",
        "VOICE_AGENT_PROVIDER",
        "ELEVENLABS_AGENT_ID",
        "ELEVENLABS_VOICE_ID",
    }

    def keys(self) -> set[str]:
        text = (ROOT / ".env.example").read_text()
        return set(re.findall(r"^([A-Z][A-Z0-9_]+)=", text, flags=re.MULTILINE))

    def test_contains_every_key_from_the_spec(self):
        assert self.SPEC_KEYS <= self.keys()

    def test_extra_keys_are_documented_settings_fields(self):
        assert self.keys() - self.SPEC_KEYS <= {
            "STORAGE_BACKEND",
            "LLM_PROVIDER",
            "LLM_BASE_URL",
            "LOCAL_STORAGE_DIR",
        }
        assert all(key in Settings.model_fields for key in self.keys())

    def test_contains_only_fake_values(self):
        text = (ROOT / ".env.example").read_text()
        assert "AKIAXXXXXXXXXXXXXXXX" in text and "change-me" in text
        assert not re.search(r"AKIA[A-Z0-9]{16}", text.replace("AKIAXXXXXXXXXXXXXXXX", ""))

    def test_dot_env_is_git_ignored_and_example_is_not(self):
        ignore = (ROOT / ".gitignore").read_text().splitlines()
        assert ".env" in ignore and ".env.example" not in ignore


class TestDeployConfig:
    def test_render_blueprint_is_valid_and_production_safe(self):
        import yaml

        blueprint = yaml.safe_load((ROOT / "render.yaml").read_text())
        service = blueprint["services"][0]
        env = {e["key"]: e for e in service["envVars"]}
        assert service["healthCheckPath"] == "/healthz"
        assert service["startCommand"].startswith("alembic upgrade head && uvicorn app.main:app")
        assert env["ENV"]["value"] == "production" and env["STORAGE_BACKEND"]["value"] == "s3"
        assert (
            env["LLM_PROVIDER"]["value"] == "gemini"
            and env["STT_PROVIDER"]["value"] == "elevenlabs"
        )
        for secret in (
            "SECRET_KEY",
            "DATABASE_URL",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "LLM_API_KEY",
            "ELEVENLABS_API_KEY",
            "ADMIN_BOOTSTRAP_PASSWORD",
        ):
            assert env[secret].get("sync") is False and "value" not in env[secret], secret
        assert all(key.upper() in Settings.model_fields or key == "PYTHON_VERSION" for key in env)

    def test_runtime_requirements_are_pinned_and_dev_tools_are_separate(self):
        runtime = (ROOT / "requirements.txt").read_text()
        dev = (ROOT / "requirements-dev.txt").read_text()
        packages = [line for line in runtime.splitlines() if line and not line.startswith("#")]
        assert packages and all("==" in line for line in packages)
        assert not re.search(
            r"^(pytest|moto|black|ruff)", runtime, flags=re.MULTILINE | re.IGNORECASE
        )
        assert "-r requirements.txt" in dev and "pytest==" in dev and "moto" in dev
