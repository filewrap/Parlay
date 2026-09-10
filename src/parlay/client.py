"""Telethon client construction and non-interactive authorized startup.

Prefer the configured SQLite session file, then a single .session file in the
working directory. Otherwise use TELEGRAM_STRING_SESSION or parse TELEGRAM_SESSION
as a portable Telethon session. Ambiguous files and invalid serialized credentials
fail with safe errors. Never log session configuration values.
"""

from __future__ import annotations

import logging
import os
import re
import struct
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

from .config import Config, ConfigError

log = logging.getLogger(__name__)


class AuthorizationError(RuntimeError):
    """Raised when the client cannot reach an authorized state non-interactively."""


def _existing_session_file(name: str) -> str | None:
    """Match Telethon's suffix rule, then discover a single supplied database."""
    filename = name if name.endswith(".session") else name + ".session"
    # isfile returns False for overlong paths, including pasted credentials.
    if name and os.path.isfile(filename):
        return filename
    candidates = [path for path in Path.cwd().glob("*.session") if path.is_file()]
    if len(candidates) > 1:
        raise ConfigError(
            "Multiple .session files found. Set TELEGRAM_SESSION to the intended file path."
        )
    return str(candidates[0]) if candidates else None


def _as_string_session(value: str) -> StringSession | None:
    """Parse locally without authenticating or exposing the credential."""
    if not value:
        return None
    try:
        session = StringSession(value)
    except (ValueError, struct.error):
        return None
    return session if session.auth_key is not None else None


def _invalid_session() -> ConfigError:
    return ConfigError(
        "Invalid or unsupported Telegram string session. Generate a Telethon "
        "StringSession and set TELEGRAM_STRING_SESSION, or supply a Telethon .session file. "
        "Session strings from other libraries are not interchangeable."
    )


def build_client(config: Config) -> TelegramClient:
    """Select a supplied file before portable credentials, without network I/O.

    Explicit file paths disambiguate discovery in the working directory.
    A normal unused filename remains supported for non-interactive startup to
    report an unauthorized session. Serialized-looking invalid input never goes
    to SQLite. StringSession retains connection/authentication data, not the
    persistent entity cache supplied by SQLiteSession.
    """
    name = config.session.strip()
    session: StringSession | str
    if (filename := _existing_session_file(name)) is not None:
        session = filename
        log.info("using existing on-disk Telegram session")
    elif config.string_session:
        parsed = _as_string_session(config.string_session.strip())
        if parsed is None:
            raise _invalid_session()
        session = parsed
        log.info("using portable StringSession for authentication")
    elif (parsed := _as_string_session(name)) is not None:
        session = parsed
        log.info("using StringSession supplied through TELEGRAM_SESSION")
    else:
        # Portable credentials are long URL-safe base64 values. Also reject an
        # overlong filename component, rather than leaking it through SQLite.
        if not name or len(os.fsencode(Path(name).name)) > 255 or (
            len(name) >= 300 and re.fullmatch(r"[A-Za-z0-9_=\-]+", name)
        ):
            raise _invalid_session()
        session = name
        log.info("creating on-disk Telegram session")
    return TelegramClient(session, config.api_id, config.api_hash)


async def start_authorized(client: TelegramClient) -> None:
    """Connect and ensure authorization without interactive login prompts."""
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
