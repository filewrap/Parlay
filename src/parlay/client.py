"""Telethon client construction and authorized startup.

This module owns how the `TelegramClient` is built and brought to an authorized
state. It is isolated from `app.py` so the client wiring is testable and so the
choice between a portable `StringSession` and an on-disk session file lives in
one place.

Session resolution order in `build_client`:
1. `TELEGRAM_STRING_SESSION`, when set, always wins (wrapped in `StringSession`).
2. If a `.session` file for `TELEGRAM_SESSION` already exists on disk (the
   user put one there), it is used as an SQLite file session.
3. If no such file exists but the `TELEGRAM_SESSION` value itself parses as a
   Telethon string session, it is wrapped in `StringSession`. This catches the
   common misconfiguration of exporting the session string under the wrong
   variable, which would otherwise make sqlite3 treat the whole string as a
   database filename and crash with 'unable to open database file'.
4. Otherwise the value is a session file name for Telethon to create.

Telethon is a pure-Python dependency, so importing it here is safe in CI. The
network calls (`connect`, `start`, `sign_in`) only run when `start_authorized`
is invoked against a real account; unit tests drive fakes instead.
"""

from __future__ import annotations

import logging
import os

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

from .config import Config

log = logging.getLogger(__name__)


class AuthorizationError(RuntimeError):
    """Raised when the client cannot reach an authorized state non-interactively."""


def _session_file_exists(name: str) -> bool:
    """True when the on-disk session database for `name` already exists.

    Telethon appends '.session' to names that lack it, so check both forms.
    """
    if os.path.isfile(name):
        return True
    return not name.endswith(".session") and os.path.isfile(name + ".session")


def _as_string_session(value: str) -> StringSession | None:
    """Parse `value` as a Telethon string session, or None if it is not one.

    A real parse attempt beats shape heuristics: `StringSession` validates the
    version byte and unpacks the auth key, so ordinary file names fail cleanly.
    """
    # File names are short; every real string session is far longer.
    if len(value) < 100:
        return None
    try:
        session = StringSession(value)
    except Exception:
        return None
    if session.auth_key is None:
        return None
    return session


def build_client(config: Config) -> TelegramClient:
    """Construct a TelegramClient from config.

    A `TELEGRAM_STRING_SESSION` takes precedence (portable, ideal for ephemeral
    hosts). Otherwise an existing `.session` file the user put on disk is used.
    If there is no file and the configured session value itself is a string
    session, it is wrapped in `StringSession` instead of being passed to
    sqlite3 as a filename. Both persist the auth key and the entity cache, so
    the account only signs in once.
    """
    session: StringSession | str
    if config.string_session:
        session = StringSession(config.string_session)
        log.info("using portable StringSession for authentication")
    elif _session_file_exists(config.session):
        session = config.session
        log.info("using existing on-disk session file %r", config.session)
    elif (parsed := _as_string_session(config.session)) is not None:
        session = parsed
        log.info(
            "TELEGRAM_SESSION value is a string session; wrapping it in "
            "StringSession instead of treating it as a file name"
        )
    else:
        session = config.session
        log.info("creating on-disk session file %r", config.session)
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
