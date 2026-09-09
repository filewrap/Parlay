"""Telethon client construction and authorized startup.

This module owns how the `TelegramClient` is built and brought to an authorized
state. It is isolated from `app.py` so the client wiring is testable and so the
choice between a portable `StringSession` and an on-disk session file lives in
one place.

Telethon is a pure-Python dependency, so importing it here is safe in CI. The
network calls (`connect`, `start`, `sign_in`) only run when `start_authorized`
is invoked against a real account; unit tests drive fakes instead.
"""

from __future__ import annotations

import logging

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

from .config import Config

log = logging.getLogger(__name__)


class AuthorizationError(RuntimeError):
    """Raised when the client cannot reach an authorized state non-interactively."""


def build_client(config: Config) -> TelegramClient:
    """Construct a TelegramClient from config.

    A `TELEGRAM_STRING_SESSION` takes precedence (portable, ideal for ephemeral
    hosts); otherwise a named on-disk session file is used. Both persist the
    auth key and the entity cache, so the account only signs in once.
    """
    session: StringSession | str
    if config.string_session:
        session = StringSession(config.string_session)
        log.info("using portable StringSession for authentication")
    else:
        session = config.session
        log.info("using on-disk session file %r", config.session)
    return TelegramClient(session, config.api_id, config.api_hash)


async def start_authorized(client: TelegramClient) -> None:
    """Connect and ensure the client is authorized, without interactive prompts.

    Parlay runs unattended, so a fresh login (which needs a login code, and
    possibly a 2FA password) cannot be completed here. In that case we raise
    `AuthorizationError` telling the Operator to generate a session first. An
    already-authorized session simply connects.
    """
    await client.connect()
    try:
        if await client.is_user_authorized():
            return
        raise AuthorizationError(
            "Telegram session is not authorized. Generate a session string "
            "(e.g. with Telethon's StringSession login) and set "
            "TELEGRAM_STRING_SESSION, or sign in once to create the session file."
        )
    except SessionPasswordNeededError as exc:  # pragma: no cover - needs live 2FA
        raise AuthorizationError(
            "This account has two-factor authentication enabled; complete the "
            "login out-of-band and provide the resulting session."
        ) from exc
