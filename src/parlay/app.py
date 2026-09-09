"""Application wiring: builds the Telethon client, registers commands, runs.

This is the control skeleton. Command handlers here return status text and
drive the CallSessionManager. Actual audio join/capture/playback is wired in by
later work orders (WO-2, WO-3); music handlers by WO-5/WO-6.
"""

from __future__ import annotations

import logging

from telethon import TelegramClient, events

from .commands import CommandHandler, MUSIC_COMMANDS, ParsedCommand
from .config import Config
from .session import CallSessionManager, SessionError

log = logging.getLogger(__name__)

_MUSIC_PENDING = "That command is not available yet (implemented in a later work order)."


class ParlayApp:
    """Owns the client, command handler, and session manager."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.client = TelegramClient(config.session, config.api_id, config.api_hash)
        self.sessions = CallSessionManager()
        self.commands = CommandHandler(config.operator_id, config.command_prefix)
        self._register_commands()

    def _register_commands(self) -> None:
        self.commands.register("join", self._cmd_join)
        self.commands.register("leave", self._cmd_leave)
        self.commands.register("start", self._cmd_start)
        self.commands.register("stop", self._cmd_stop)
        self.commands.register("status", self._cmd_status)
        for name in MUSIC_COMMANDS:
            self.commands.register(name, self._cmd_music_pending)

    async def _cmd_join(self, command: ParsedCommand) -> str:
        target = command.args or "the current chat"
        try:
            self.sessions.begin_join(target)
        except SessionError as exc:
            return str(exc)
        # Audio join via py-tgcalls is wired in WO-2; mark connected for now.
        self.sessions.mark_connected()
        return f"Joined {target}."

    async def _cmd_leave(self, command: ParsedCommand) -> str:
        try:
            self.sessions.end()
        except SessionError as exc:
            return str(exc)
        return "Left the voice chat."

    async def _cmd_start(self, command: ParsedCommand) -> str:
        try:
            self.sessions.engage_ai()
        except SessionError as exc:
            return str(exc)
        return "AI voice pipeline engaged."

    async def _cmd_stop(self, command: ParsedCommand) -> str:
        try:
            self.sessions.disengage_ai()
        except SessionError as exc:
            return str(exc)
        return "AI voice pipeline stopped."

    async def _cmd_status(self, command: ParsedCommand) -> str:
        return self.sessions.status_text()

    async def _cmd_music_pending(self, command: ParsedCommand) -> str:
        return _MUSIC_PENDING

    async def _on_message(self, event: events.NewMessage.Event) -> None:
        reply = await self.commands.dispatch(event.raw_text, event.sender_id)
        if reply is not None:
            await event.reply(reply)

    async def run(self) -> None:
        """Start the client and process messages until disconnected."""
        self.client.add_event_handler(self._on_message, events.NewMessage())
        await self.client.start()
        me = await self.client.get_me()
        log.info("Parlay is online as %s", getattr(me, "username", None) or me.id)
        await self.client.run_until_disconnected()
