"""Configuration loading for Parlay.

All runtime configuration comes from environment variables (optionally loaded
from a local .env file). No secrets are hard-coded.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or malformed."""


@dataclass(frozen=True)
class Config:
    """Immutable snapshot of Parlay's runtime configuration."""

    api_id: int
    api_hash: str
    session: str
    string_session: str | None
    operator_id: str
    gemini_api_key: str
    pot_provider_url: str
    command_prefix: str
    log_level: str
    gemini_model: str | None
    gemini_voice: str | None
    gemini_persona: str | None
    audit_log_path: str


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def _optional(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def load_config() -> Config:
    """Load and validate configuration from the environment.

    Reads a .env file if present, then validates required keys.
    """
    load_dotenv()

    raw_api_id = _require("TELEGRAM_API_ID")
    try:
        api_id = int(raw_api_id)
    except ValueError as exc:
        raise ConfigError("TELEGRAM_API_ID must be an integer") from exc

    return Config(
        api_id=api_id,
        api_hash=_require("TELEGRAM_API_HASH"),
        session=os.environ.get("TELEGRAM_SESSION", "parlay.session").strip(),
        # A portable StringSession, when provided, takes precedence over the
        # on-disk session file (ideal for ephemeral hosts). See client.py.
        string_session=_optional("TELEGRAM_STRING_SESSION"),
        operator_id=_require("OPERATOR_ID"),
        gemini_api_key=_require("GEMINI_API_KEY"),
        pot_provider_url=os.environ.get("POT_PROVIDER_URL", "http://127.0.0.1:4416").strip(),
        command_prefix=os.environ.get("COMMAND_PREFIX", "/").strip() or "/",
        log_level=os.environ.get("LOG_LEVEL", "INFO").strip().upper(),
        gemini_model=_optional("GEMINI_MODEL"),
        gemini_voice=_optional("GEMINI_VOICE"),
        gemini_persona=_optional("GEMINI_PERSONA"),
        audit_log_path=os.environ.get("AUDIT_LOG_PATH", "parlay-membership.log").strip()
        or "parlay-membership.log",
    )
