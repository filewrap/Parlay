"""Environment configuration for the Linux backend and Telegram clients."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

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
    max_concurrent_calls: int = 4
    bot_token: str | None = None
    bot_session: str = "data/parlay-bot"
    bot_db_path: str = "data/parlay-bot.sqlite3"
    bot_username: str | None = None
    mini_app_url: str | None = None
    mini_app_short_name: str | None = None
    room_db_path: str = "data/parlay-rooms.sqlite3"
    allowed_origins: tuple[str, ...] = ()
    backend_host: str = "127.0.0.1"
    backend_port: int = 8080
    ml_db_path: str = "data/parlay-ml.sqlite3"
    ml_model_dir: str = "data/models"
    youtube_api_key: str | None = None


def _require(name: str) -> str:
    value = _optional(name)
    if value is None:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def _optional(name: str) -> str | None:
    return os.environ.get(name, "").strip() or None


def _integer(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(_optional(name) or default)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if not low <= value <= high:
        raise ConfigError(f"{name} must be from {low} to {high}")
    return value


def load_config() -> Config:
    load_dotenv()
    try:
        api_id = int(_require("TELEGRAM_API_ID"))
    except ValueError as exc:
        raise ConfigError("TELEGRAM_API_ID must be an integer") from exc
    token, mini_app = _optional("BOT_TOKEN"), _optional("MINI_APP_URL")
    origins = tuple(x.strip() for x in (_optional("ALLOWED_ORIGINS") or "").split(",") if x.strip())
    if token:
        url = urlsplit(mini_app or "")
        if url.scheme != "https" or not url.hostname or url.username or url.fragment:
            raise ConfigError("BOT_TOKEN requires an HTTPS MINI_APP_URL")
        if not origins:
            origins = (f"{url.scheme}://{url.netloc}",)
        for origin in origins:
            parsed = urlsplit(origin)
            local = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}
            if (
                not parsed.hostname
                or parsed.username
                or parsed.path
                or parsed.query
                or parsed.fragment
                or (parsed.scheme != "https" and not local)
                or "*" in origin
            ):
                raise ConfigError("ALLOWED_ORIGINS must contain exact HTTPS origins")
    return Config(
        api_id=api_id,
        api_hash=_require("TELEGRAM_API_HASH"),
        session=os.environ.get("TELEGRAM_SESSION", "parlay.session").strip(),
        string_session=_optional("TELEGRAM_STRING_SESSION"),
        operator_id=_require("OPERATOR_ID"),
        gemini_api_key=_optional("GEMINI_API_KEY") or "",
        pot_provider_url=_optional("POT_PROVIDER_URL") or "http://127.0.0.1:4416",
        command_prefix=_optional("COMMAND_PREFIX") or "/",
        log_level=(_optional("LOG_LEVEL") or "INFO").upper(),
        gemini_model=_optional("GEMINI_MODEL"),
        gemini_voice=_optional("GEMINI_VOICE"),
        gemini_persona=_optional("GEMINI_PERSONA"),
        audit_log_path=_optional("AUDIT_LOG_PATH") or "parlay-membership.log",
        activity_db_path=_optional("ACTIVITY_DB_PATH") or "data/parlay.sqlite3",
        max_concurrent_calls=_integer("MAX_CONCURRENT_CALLS", 4, 2, 64),
        bot_token=token,
        bot_session=_optional("BOT_SESSION") or "data/parlay-bot",
        bot_db_path=_optional("BOT_DB_PATH") or "data/parlay-bot.sqlite3",
        bot_username=_optional("BOT_USERNAME"),
        mini_app_url=mini_app,
        mini_app_short_name=_optional("MINI_APP_SHORT_NAME"),
        room_db_path=_optional("ROOM_DB_PATH") or "data/parlay-rooms.sqlite3",
        allowed_origins=origins,
        backend_host=_optional("BACKEND_HOST") or "127.0.0.1",
        backend_port=_integer("BACKEND_PORT", 8080, 1, 65535),
        ml_db_path=_optional("ML_DB_PATH") or "data/parlay-ml.sqlite3",
        ml_model_dir=_optional("ML_MODEL_DIR") or "data/models",
        youtube_api_key=_optional("YOUTUBE_API_KEY"),
    )
