"""Configuration validation does not require fixed frontend hosting hardware."""

import pytest

from parlay.config import ConfigError, load_config


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr("parlay.config.load_dotenv", lambda: None)
    for key in (
        "BOT_TOKEN",
        "MINI_APP_URL",
        "ALLOWED_ORIGINS",
        "MAX_CONCURRENT_CALLS",
        "BACKEND_PORT",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "test")
    monkeypatch.setenv("OPERATOR_ID", "7")
    return monkeypatch


def test_userbot_only_compatible(env):
    config = load_config()
    assert config.bot_token is None
    assert config.max_concurrent_calls >= 2


def test_bot_requires_host_url_and_derives_origin(env):
    env.setenv("BOT_TOKEN", "test")
    with pytest.raises(ConfigError):
        load_config()
    env.setenv("MINI_APP_URL", "https://rooms.example.test")
    assert load_config().allowed_origins == ("https://rooms.example.test",)
    env.setenv("ALLOWED_ORIGINS", "https://*.example.test")
    with pytest.raises(ConfigError):
        load_config()
