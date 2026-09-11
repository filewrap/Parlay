"""Telethon command boundary, independent of playback and activity tracking."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import Any

from telethon import events
from telethon.errors import FloodWaitError, RPCError

from . import presentation as fmt
from .commands import CommandHandler
from .media.track import MediaError

log = logging.getLogger(__name__)

# Commands slow enough to warrant a live status message that is edited in place
# rather than a reply that only appears once the work is done.
_PROGRESS_COMMANDS = frozenset({"play"})
_PROGRESS_TEXT = fmt.status("Searching for your track\u2026", "queue")


class TelegramCommandWrapper:
    """Preserve event context, reject forwards, and dispatch each message once.

    Sender identity, not the outgoing flag, grants access. Anonymous/channel
    posts are not attributed to an operator. Incoming and outgoing messages
    both reach this wrapper. No raw command content is logged.
    """

    def __init__(self, client: Any, commands: CommandHandler) -> None:
        self.client = client
        self.commands = commands
        self._seen: OrderedDict[tuple[int, int], None] = OrderedDict()
        self._locks: dict[int, asyncio.Lock] = {}

    def install(self) -> None:
        self.client.add_event_handler(self.handle, events.NewMessage(forwards=False))

    def uninstall(self) -> None:
        self.client.remove_event_handler(self.handle)

    async def handle(self, event: Any) -> None:
        if getattr(event, "fwd_from", None) is not None:
            return
        if not self.commands.is_operator(event.sender_id):
            return
        parsed = self.commands.parse(event.raw_text or "", event.sender_id)
        if parsed is None:
            return
        if event.chat_id is None:
            return
        key = (int(event.chat_id), int(event.id))
        # Mark before awaiting, so duplicate deliveries cannot enter twice.
        if key in self._seen:
            return
        self._seen[key] = None
        while len(self._seen) > 2048:
            self._seen.popitem(last=False)
        async with self._locks.setdefault(int(event.chat_id), asyncio.Lock()):
            # For slow commands, show a status message immediately and edit it in
            # place with the outcome, so /play never looks unresponsive.
            progress = None
            if parsed.name in _PROGRESS_COMMANDS:
                progress = await self._send_progress(event)
            try:
                async with asyncio.timeout(90):
                    reply = await self.commands.dispatch(
                        event.raw_text,
                        event.sender_id,
                        chat_id=event.chat_id,
                        is_group=bool(event.is_group),
                        is_channel=bool(event.is_channel),
                    )
            except FloodWaitError as exc:
                log.warning("Telegram requested a command cooldown of %s seconds", exc.seconds)
                reply = fmt.error("Telegram asked Parlay to slow down. Try that again shortly.")
                if progress is None:
                    return
            except MediaError:
                log.warning("Media resolution or playback failed", exc_info=True)
                reply = fmt.error(
                    "The media source could not provide this track. "
                    "It may be blocked or unavailable. Check the provider logs."
                )
            except RPCError:
                log.warning("Telegram rejected a command operation", exc_info=True)
                reply = fmt.error("Telegram rejected this operation. Check access and call status.")
            except Exception:
                log.exception("Command failed")
                reply = fmt.error("The command failed. Check the Parlay service log.")
            if reply is not None:
                await self._deliver(event, progress, reply)

    async def _send_progress(self, event: Any) -> Any | None:
        """Post the live status message over the userbot client, or None if it fails."""
        try:
            message = await event.reply(_PROGRESS_TEXT)
        except RPCError:
            log.warning("Cannot send command progress message", exc_info=True)
            return None
        self._seen[(int(event.chat_id), int(message.id))] = None
        return message

    async def _deliver(self, event: Any, progress: Any | None, reply: str) -> None:
        """Edit the live status message in place, or send a fresh reply if there is none."""
        try:
            if progress is not None:
                await progress.edit(reply)
                return
            sent = await event.reply(reply)
            self._seen[(int(event.chat_id), int(sent.id))] = None
        except RPCError:
            log.warning("Cannot send command response", exc_info=True)
