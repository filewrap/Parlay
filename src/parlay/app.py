"""Application wiring: builds the Telethon client, registers commands, runs.

The command handlers drive the CallSessionManager and the RawAudioBridge.
Join brings up the raw audio bridge; leave tears it down and releases buffers.
`/start` engages the AI Voice Pipeline over the bridge's Captured Stream and
Playback Sink; `/stop` disengages it. Music (WO-5/WO-6) plugs into the same
bridge later.

All outgoing messages are formatted through the shared presentation layer.
"""

from __future__ import annotations

import logging

from telethon import TelegramClient, events

from . import presentation as fmt
from .audio.bridge import RawAudioBridge
from .commands import MUSIC_COMMANDS, CommandHandler, ParsedCommand
from .config import Config
from .session import CallSessionManager, SessionError
from .voice.gemini import GeminiVoiceProvider
from .voice.provider import ProviderError
from .voice.session_manager import ProviderSessionManager

log = logging.getLogger(__name__)

_MUSIC_PENDING = "That command is not available yet (implemented in a later work order)."


class ParlayApp:
    """Owns the client, command handler, session manager, and audio bridge."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.client = TelegramClient(config.session, config.api_id, config.api_hash)
        self.sessions = CallSessionManager()
        self.commands = CommandHandler(config.operator_id, config.command_prefix)
        self.bridge: RawAudioBridge | None = None
        self.pipeline: ProviderSessionManager | None = None
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
            return fmt.error(str(exc))
        # Bring up the raw audio bridge for this session.
        bridge = RawAudioBridge(self.client)
        try:
            await bridge.start(target)
        except Exception as exc:  # roll back the session on join failure
            log.exception("failed to start raw audio bridge")
            self.sessions.end()
            return fmt.error(f"Could not join {target}: {exc}")
        self.bridge = bridge
        self.sessions.mark_connected()
        return fmt.success(f"Joined {target}.")

    async def _cmd_leave(self, command: ParsedCommand) -> str:
        try:
            self.sessions.end()
        except SessionError as exc:
            return fmt.error(str(exc))
        await self._teardown_pipeline()
        if self.bridge is not None:
            await self.bridge.stop()
            self.bridge = None
        return fmt.success("Left the voice chat.")

    async def _cmd_start(self, command: ParsedCommand) -> str:
        if self.bridge is None:
            return fmt.error("Join a voice chat first.")
        try:
            self.sessions.engage_ai()
        except SessionError as exc:
            return fmt.error(str(exc))
        provider = GeminiVoiceProvider(self.config.gemini_api_key)
        pipeline = ProviderSessionManager(
            provider,
            source=self.bridge,
            sink=self.bridge,
            on_loss=self._on_pipeline_loss,
        )
        try:
            await pipeline.engage()
        except ProviderError as exc:
            # Do not leave the pipeline engaged if the session cannot open.
            self.sessions.disengage_ai()
            log.exception("failed to engage AI voice pipeline")
            return fmt.error(f"Could not engage the AI pipeline: {exc}")
        self.pipeline = pipeline
        return fmt.success("AI voice pipeline engaged.")

    async def _cmd_stop(self, command: ParsedCommand) -> str:
        try:
            self.sessions.disengage_ai()
        except SessionError as exc:
            return fmt.error(str(exc))
        await self._teardown_pipeline()
        return fmt.success("AI voice pipeline stopped.")

    async def _cmd_status(self, command: ParsedCommand) -> str:
        icon_key = "connected" if self.sessions.active else "idle"
        return fmt.status(self.sessions.status_text(), icon_key)

    async def _cmd_music_pending(self, command: ParsedCommand) -> str:
        return fmt.warning(_MUSIC_PENDING)

    async def _teardown_pipeline(self) -> None:
        if self.pipeline is not None:
            await self.pipeline.disengage()
            self.pipeline = None

    async def _on_pipeline_loss(self, reason: str) -> None:
        """Called when the pipeline disengages itself after an unrecoverable loss."""
        self.pipeline = None
        try:
            self.sessions.disengage_ai()
        except SessionError:
            pass
        session = self.sessions.session
        if session is not None:
            try:
                await self.client.send_message(
                    session.chat, fmt.error(f"AI voice pipeline stopped: {reason}")
                )
            except Exception:
                log.exception("failed to notify Operator of pipeline loss")

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
