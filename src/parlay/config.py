"""Environment configuration. Secrets are never hard-coded."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """A required configuration value is missing or malformed."""


@dataclass(frozen=True)
class Config:
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
    activity_db_path: str = "data/parlay.sqlite3"


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def _optional(name: str) -> str | None:
    return os.environ.get(name, "").strip() or None


def load_config() -> Config:
    load_dotenv()
    try:
        api_id = int(_require("TELEGRAM_API_ID"))
    except ValueError as exc:
        raise ConfigError("TELEGRAM_API_ID must be an integer") from exc
    return Config(
        api_id=api_id,
        api_hash=_require("TELEGRAM_API_HASH"),
        session=os.environ.get("TELEGRAM_SESSION", "parlay.session").strip(),
        string_session=_optional("TELEGRAM_STRING_SESSION"),
        operator_id=_require("OPERATOR_ID"),
        gemini_api_key=_require("GEMINI_API_KEY"),
        pot_provider_url=_optional("POT_PROVIDER_URL") or "http://127.0.0.1:4416",
        command_prefix=_optional("COMMAND_PREFIX") or "/",
        log_level=(_optional("LOG_LEVEL") or "INFO").upper(),
        gemini_model=_optional("GEMINI_MODEL"),
        gemini_voice=_optional("GEMINI_VOICE"),
        gemini_persona=_optional("GEMINI_PERSONA"),
        audit_log_path=_optional("AUDIT_LOG_PATH") or "parlay-membership.log",
        activity_db_path=_optional("ACTIVITY_DB_PATH") or "data/parlay.sqlite3",
    )
