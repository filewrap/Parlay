"""Application wiring: builds the Telethon client, registers commands, runs.

The command handlers drive the CallSessionManager and the RawAudioBridge.
Join brings up the raw audio bridge, the Audio Output Arbiter, and the music
controller; leave tears them down and releases buffers. `/start` engages the AI
Voice Pipeline as a producer behind the Arbiter over the bridge's Captured
Stream and Playback Sink; `/stop` is context-aware: it stops music when music
is playing, otherwise it disengages the AI pipeline. Music and AI share the one
Arbiter, so they never play at once (REQ-INJ-006).

The music commands (play/skip/pause/resume/queue) are handled by the
MusicController, which resolves tracks through the Media Sourcing Pipeline and
plays them through the Arbiter.

The Group Membership Audit watches the same user session for ChatAction updates
that add or remove Parlay's own account, privately notifying the Operator and
appending to an Operator-local audit log.

An unexpected call drop is reported to the Operator and ends the session so a
later join can succeed (REQ-BOT-006).

All outgoing messages are formatted through the shared presentation layer.

Telethon usage: the client is built and authorized in `client.py`; commands are
received through an `events.NewMessage` handler filtered to the Operator via
`from_users` (so non-operator messages never reach the handler), and membership
changes through `events.ChatAction`. Entities (the Operator, call chats) are
resolved with `get_input_entity` before use so Telegram requests carry a valid
InputPeer.
"""

from __future__ import annotations

import logging
from typing import Any

from telethon import events

from . import presentation as fmt
from .audio.arbiter import AudioOutputArbiter
from .audio.bridge import RawAudioBridge
from .client import build_client, start_authorized
from .commands import CommandHandler, ParsedCommand
from .config import Config
from .media.po_token import PoTokenProvider
from .media.resolver import TrackResolver
from .media.source_selector import SourceSelector
from .media.transcoder import MediaTranscoder
from .membership.notifier import MembershipNotifier
from .membership.store import AuditLogStore
from .membership.watcher import MembershipWatcher
from .music.controller import MusicController
from .session import CallSessionManager, SessionError
from .voice.ai_producer import AiVoiceProducer
from .voice.gemini import GeminiVoiceProvider, default_configuration
from .voice.provider import ProviderError, SessionConfiguration

log = logging.getLogger(__name__)


class ParlayApp:
    """Owns the client, command handler, session manager, and audio bridge."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.client = build_client(config)
        self.sessions = CallSessionManager()
        self.commands = CommandHandler(config.operator_id, config.command_prefix)
        self.bridge: RawAudioBridge | None = None
        self.arbiter: AudioOutputArbiter | None = None
        self.ai: AiVoiceProducer | None = None
        self.music: MusicController | None = None
        self.membership: MembershipWatcher | None = None
        # Resolved InputPeer for the Operator; set at startup and reused for
        # every private notification so no repeat resolution is needed.
        self._operator_peer: Any | None = None
        self._register_commands()

    def _register_commands(self) -> None:
        self.commands.register("join", self._cmd_join)
        self.commands.register("leave", self._cmd_leave)
        self.commands.register("start", self._cmd_start)
        self.commands.register("stop", self._cmd_stop)
        self.commands.register("status", self._cmd_status)
        self.commands.register("play", self._cmd_play)
        self.commands.register("skip", self._cmd_skip)
        self.commands.register("pause", self._cmd_pause)
        self.commands.register("resume", self._cmd_resume)
        self.commands.register("queue", self._cmd_queue)

    async def _cmd_join(self, command: ParsedCommand) -> str:
        target = command.args or "the current chat"
        try:
            self.sessions.begin_join(target)
        except SessionError as exc:
            return fmt.error(str(exc))
        # Bring up the raw audio bridge for this session, wiring drop detection.
        bridge = RawAudioBridge(self.client, on_disconnect=self._on_call_dropped)
        try:
            await bridge.start(target)
        except Exception as exc:  # roll back the session on join failure
            log.exception("failed to start raw audio bridge")
            self.sessions.end()
            return fmt.error(f"Could not join {target}: {exc}")
        self.bridge = bridge
        # The Arbiter owns the single call output; producers request it through
        # the Arbiter so AI and music never mix (REQ-INJ-006).
        self.arbiter = AudioOutputArbiter(bridge)
        self.music = self._build_music(self.arbiter)
        self.sessions.mark_connected()
        return fmt.success(f"Joined {target}.")

    def _build_music(self, arbiter: AudioOutputArbiter) -> MusicController:
        """Assemble the Media Sourcing Pipeline and MusicController for a session."""
        po_tokens = PoTokenProvider(self.config.pot_provider_url)
        selector = SourceSelector(po_tokens)
        resolver = TrackResolver(selector)
        transcoder = MediaTranscoder()
        return MusicController(
            self.sessions,
            arbiter,
            resolver,
            transcoder,
            post_message=self._post_to_call,
        )

    def _ai_configuration(self) -> SessionConfiguration:
        """Build the Session Configuration from config, over the default.

        Any of model / voice / persona left unset falls back to the provider's
        native-audio default (REQ-AIVP-007.2/.3).
        """
        default = default_configuration()
        return SessionConfiguration(
            model=self.config.gemini_model or default.model,
            system_instruction=self.config.gemini_persona or default.system_instruction,
            voice=self.config.gemini_voice or default.voice,
            response_modality=default.response_modality,
        )

    async def _post_to_call(self, text: str) -> None:
        """Post an in-call message to the active Call Session's chat."""
        session = self.sessions.session
        if session is None:
            return
        await self.client.send_message(session.chat, text)

    async def _notify_operator(self, text: str) -> None:
        """Send a private message to the Operator's own account.

        Membership alerts go here and never into a triggering chat (ADR-001).
        Uses the InputPeer resolved at startup, falling back to the configured
        id so a notification is never dropped.
        """
        target = self._operator_peer if self._operator_peer is not None else self.config.operator_id
        await self.client.send_message(target, text)

    async def _on_ai_speaking(self) -> None:
        """Announce in-call that the AI is speaking (REQ-AIVP-008.1)."""
        await self._post_to_call(fmt.status("The AI is speaking.", "speaking"))

    async def _cmd_leave(self, command: ParsedCommand) -> str:
        try:
            self.sessions.end()
        except SessionError as exc:
            return fmt.error(str(exc))
        await self._teardown_pipeline()
        await self._teardown_bridge()
        return fmt.success("Left the voice chat.")

    async def _cmd_start(self, command: ParsedCommand) -> str:
        if self.bridge is None or self.arbiter is None:
            return fmt.error("Join a voice chat first.")
        try:
            self.sessions.engage_ai()
        except SessionError as exc:
            return fmt.error(str(exc))
        provider = GeminiVoiceProvider(self.config.gemini_api_key, self._ai_configuration())
        ai = AiVoiceProducer(
            provider,
            source=self.bridge,
            arbiter=self.arbiter,
            on_loss=self._on_pipeline_loss,
            on_speaking=self._on_ai_speaking,
        )
        try:
            await ai.engage()
        except ProviderError as exc:
            # Do not leave the pipeline engaged if the session cannot open.
            self.sessions.disengage_ai()
            log.exception("failed to engage AI voice pipeline")
            return fmt.error(f"Could not engage the AI pipeline: {exc}")
        self.ai = ai
        return fmt.success("AI voice pipeline engaged.")

    async def _cmd_stop(self, command: ParsedCommand) -> str:
        # Context-aware: stop music when music is playing, else disengage the AI.
        if self.music is not None and self.music.is_active:
            return await self.music.stop()
        try:
            self.sessions.disengage_ai()
        except SessionError as exc:
            return fmt.error(str(exc))
        await self._teardown_pipeline()
        return fmt.success("AI voice pipeline stopped.")

    async def _cmd_status(self, command: ParsedCommand) -> str:
        icon_key = "connected" if self.sessions.active else "idle"
        return fmt.status(self.sessions.status_text(), icon_key)

    async def _cmd_play(self, command: ParsedCommand) -> str:
        if self.music is None:
            return fmt.error("Join a voice chat first.")
        return await self.music.play(command.args)

    async def _cmd_skip(self, command: ParsedCommand) -> str:
        if self.music is None:
            return fmt.error("Join a voice chat first.")
        return await self.music.skip()

    async def _cmd_pause(self, command: ParsedCommand) -> str:
        if self.music is None:
            return fmt.error("Join a voice chat first.")
        return await self.music.pause()

    async def _cmd_resume(self, command: ParsedCommand) -> str:
        if self.music is None:
            return fmt.error("Join a voice chat first.")
        return await self.music.resume()

    async def _cmd_queue(self, command: ParsedCommand) -> str:
        if self.music is None:
            return fmt.error("Join a voice chat first.")
        return await self.music.show_queue()

    async def _teardown_pipeline(self) -> None:
        if self.ai is not None:
            await self.ai.disengage()
            self.ai = None

    async def _teardown_bridge(self) -> None:
        if self.music is not None:
            await self.music.on_session_end()
            self.music = None
        if self.bridge is not None:
            await self.bridge.stop()
            self.bridge = None
        self.arbiter = None

    async def _on_call_dropped(self) -> None:
        """Handle an unexpected voice-chat disconnect (REQ-BOT-006).

        End the Call Session, tear down the AI pipeline and bridge to release
        resources so a later join succeeds, and report the drop to the Operator.
        """
        if not self.sessions.active:
            return
        chat = self.sessions.session.chat if self.sessions.session else None
        log.warning("voice chat connection dropped; ending session")
        await self._teardown_pipeline()
        await self._teardown_bridge()
        try:
            self.sessions.end()
        except SessionError:
            pass
        if chat is not None:
            try:
                await self.client.send_message(
                    chat, fmt.error("Voice chat connection dropped; the session has ended.")
                )
            except Exception:
                log.exception("failed to notify Operator of call drop")

    async def _on_pipeline_loss(self, reason: str) -> None:
        """Called when the pipeline disengages itself after an unrecoverable loss."""
        self.ai = None
        try:
            self.sessions.disengage_ai()
        except SessionError:
            pass
        session = self.sessions.session
        if session is not None:
            try:
                await self.client.send_message(
                    session.chat, fmt.error(f"The AI encountered an error: {reason}")
                )
            except Exception:
                log.exception("failed to notify Operator of pipeline loss")

    async def _on_message(self, event: events.NewMessage.Event) -> None:
        """Handle an incoming Operator command.

        The handler is registered with a `from_users` filter, so it only fires
        for the Operator; the CommandHandler still gates defensively.
        """
        reply = await self.commands.dispatch(event.raw_text, event.sender_id)
        if reply is not None:
            await event.reply(reply)

    async def _on_chat_action(self, event: events.ChatAction.Event) -> None:
        """Route a ChatAction update to the Group Membership Audit."""
        if self.membership is None:
            return
        try:
            await self.membership.handle(event)
        except Exception:
            log.exception("failed to handle chat action for membership audit")

    def _build_membership(self, self_id: str) -> MembershipWatcher:
        """Assemble the membership notifier, audit log, and watcher."""
        notifier = MembershipNotifier(self._notify_operator)
        store = AuditLogStore(self.config.audit_log_path)
        return MembershipWatcher(self_id, [notifier, store])

    async def _resolve_operator(self) -> Any | None:
        """Resolve the configured Operator to an InputPeer for gating and DMs.

        Accepts a numeric id, username, or phone (per Telethon's entity rules).
        On success the numeric id also becomes the CommandHandler's gate so a
        username-configured Operator still matches incoming sender ids.
        """
        try:
            entity = await self.client.get_entity(self.config.operator_id)
        except (ValueError, TypeError):
            log.warning("could not resolve OPERATOR_ID %r; gating by raw id", self.config.operator_id)
            return None
        self._operator_peer = await self.client.get_input_entity(entity)
        self.commands.set_operator_id(str(entity.id))
        return entity

    async def run(self) -> None:
        """Start the client and process messages until disconnected."""
        await start_authorized(self.client)
        me = await self.client.get_me()
        operator = await self._resolve_operator()
        # Filter command messages to the Operator at the Telethon layer; fall
        # back to an unfiltered handler if the Operator could not be resolved.
        from_users = [operator] if operator is not None else None
        self.client.add_event_handler(
            self._on_message, events.NewMessage(incoming=True, from_users=from_users)
        )
        self.client.add_event_handler(self._on_chat_action, events.ChatAction())
        # The Membership Audit keys off Parlay's own account id.
        self.membership = self._build_membership(str(me.id))
        log.info("Parlay is online as %s", getattr(me, "username", None) or me.id)
        try:
            await self.client.run_until_disconnected()
        finally:
            await self.client.disconnect()
