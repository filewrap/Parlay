"""Session selection tests against real Telethon classes, without network I/O."""

from __future__ import annotations

import logging

import pytest
from telethon.crypto import AuthKey
from telethon.sessions import SQLiteSession, StringSession

from parlay.client import AuthorizationError, build_client, start_authorized
from parlay.config import Config, ConfigError


@pytest.fixture(autouse=True)
def isolated_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


def _config(**overrides: object) -> Config:
    base: dict[str, object] = dict(
        api_id=12345,
        api_hash="abc",
        session="parlay.session",
        string_session=None,
        operator_id="100",
        gemini_api_key="k",
        pot_provider_url="http://127.0.0.1:4416",
        command_prefix="/",
        log_level="INFO",
        gemini_model=None,
        gemini_voice=None,
        gemini_persona=None,
        audit_log_path="audit.log",
    )
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


def _valid_string_session(address="127.0.0.1", key=bytes(range(256))) -> str:
    """Synthetic nonzero key; never use a real account credential in tests."""
    session = StringSession()
    session.set_dc(1, address, 443)
    session.auth_key = AuthKey(key)
    return session.save()


def _file(path) -> None:
    session = SQLiteSession(str(path))
    session.close()


@pytest.mark.parametrize("field", ["session", "string_session"])
@pytest.mark.parametrize("address", ["127.0.0.1", "::1"])
def test_string_session_roundtrip(field, address, tmp_path, caplog):
    value = _valid_string_session(address)
    with caplog.at_level(logging.INFO):
        client = build_client(_config(**{field: f"  {value}  "}))
    assert isinstance(client.session, StringSession)
    assert client.session.auth_key.key == bytes(range(256))
    assert client.session.server_address == address
    assert not list(tmp_path.iterdir())
    assert value not in caplog.text


def test_explicit_string_overrides_session_value():
    value = _valid_string_session()
    client = build_client(_config(session="A" * 351, string_session=value))
    assert isinstance(client.session, StringSession)


@pytest.mark.parametrize("suffix", ["", ".session"])
def test_existing_configured_file_wins(tmp_path, suffix):
    path = tmp_path / "chosen.session"
    _file(path)
    _file(tmp_path / "other.session")
    client = build_client(
        _config(session=str(tmp_path / ("chosen" + suffix)), string_session=_valid_string_session())
    )
    try:
        assert isinstance(client.session, SQLiteSession)
        assert client.session.filename == str(path)
    finally:
        client.session.close()


def test_discover_user_supplied_file_before_string(tmp_path):
    path = tmp_path / "uploaded.session"
    _file(path)
    client = build_client(_config(session=_valid_string_session()))
    try:
        assert isinstance(client.session, SQLiteSession)
        assert client.session.filename == str(path)
    finally:
        client.session.close()


def test_ambiguous_files_require_explicit_selection(tmp_path):
    _file(tmp_path / "one.session")
    _file(tmp_path / "two.session")
    with pytest.raises(ConfigError, match="Multiple .session files"):
        build_client(_config(string_session=_valid_string_session()))


def test_session_directory_is_not_a_database(tmp_path):
    (tmp_path / "folder.session").mkdir()
    client = build_client(_config(session=_valid_string_session()))
    assert isinstance(client.session, StringSession)


@pytest.mark.parametrize("name", ["parlay", "parlay.session", "x" * 120])
def test_normal_filename_remains_supported(name, tmp_path):
    client = build_client(_config(session=str(tmp_path / name)))
    try:
        assert isinstance(client.session, SQLiteSession)
    finally:
        client.session.close()


@pytest.mark.parametrize("field", ["session", "string_session"])
@pytest.mark.parametrize(
    "value",
    ["A" * 351, "1" + "!" * 352, _valid_string_session(key=bytes(256))],
)
def test_invalid_serialized_credentials_never_reach_sqlite(field, value, tmp_path, caplog):
    with caplog.at_level(logging.INFO), pytest.raises(ConfigError) as caught:
        build_client(_config(**{field: value}))
    assert value not in str(caught.value)
    assert value not in caplog.text
    assert not list(tmp_path.iterdir())


def test_short_invalid_explicit_string_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="Invalid or unsupported"):
        build_client(_config(string_session="invalid"))
    assert not list(tmp_path.iterdir())


class FakeClient:
    def __init__(self, *, authorized: bool) -> None:
        self._authorized = authorized
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def is_user_authorized(self) -> bool:
        return self._authorized


async def test_start_authorized_returns_when_authorized() -> None:
    client = FakeClient(authorized=True)
    await start_authorized(client)  # type: ignore[arg-type]
    assert client.connected


async def test_start_authorized_raises_when_not_authorized() -> None:
    client = FakeClient(authorized=False)
    with pytest.raises(AuthorizationError):
        await start_authorized(client)  # type: ignore[arg-type]
    assert client.connected
