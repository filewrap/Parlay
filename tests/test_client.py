"""Tests for client construction and authorized startup.

`build_client` is exercised against the real Telethon classes (pure Python, no
network). `start_authorized` is exercised against a fake client so the
connect/authorize branching is tested without a live connection.
"""

from __future__ import annotations

import pytest
from telethon.crypto import AuthKey
from telethon.sessions import StringSession

from parlay.client import AuthorizationError, build_client, start_authorized
from parlay.config import Config


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


def _valid_string_session() -> str:
    """Produce a valid, non-empty StringSession string (no network needed).

    An empty StringSession saves to ''(falsy); build_client only picks the
    string session when it is truthy, so the test needs a populated one.
    """
    session = StringSession()
    session.set_dc(1, "127.0.0.1", 80)
    session.auth_key = AuthKey(bytes(256))
    return session.save()


def test_build_client_uses_string_session_when_provided() -> None:
    client = build_client(_config(string_session=_valid_string_session()))
    assert isinstance(client.session, StringSession)


def test_build_client_uses_file_session_otherwise(tmp_path) -> None:
    path = str(tmp_path / "parlay")
    client = build_client(_config(session=path))
    # A file-backed session is not a StringSession.
    assert not isinstance(client.session, StringSession)


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
