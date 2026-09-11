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
        if self.commands.parse(event.raw_text or "", event.sender_id) is None:
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
                try:
                    sent = await event.reply(reply)
                    self._seen[(int(event.chat_id), int(sent.id))] = None
                except RPCError:
                    log.warning("Cannot send command response", exc_info=True)
