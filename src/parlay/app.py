"""Wire Telegram commands, observed call activity, and the native audio runtime.

Telegram owns observed call state. ActivityTracker reconciles raw updates and
persists observations. A manual join in another Telegram client does not create
an audio transport in this process. Commands retain their originating chat.
The media runtime still supports one call; concurrent runtimes are separate work.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from telethon import events
from telethon.utils import get_peer_id

from . import presentation as fmt
from .activity import ActivityTracker
from .audio.arbiter import AudioOutputArbiter
from .audio.bridge import RawAudioBridge
from .client import build_client, start_authorized
from .command_wrapper import TelegramCommandWrapper
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
from .vc import VoiceChatController, VoiceChatError
from .voice.ai_producer import AiVoiceProducer
from .voice.gemini import GeminiVoiceProvider, default_configuration
from .voice.provider import ProviderError, SessionConfiguration

log = logging.getLogger(__name__)


class ParlayApp:
    """Own Telegram interfaces and reconcile them with the audio runtime."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.client = build_client(config)
        self.sessions = CallSessionManager()
        self.commands = CommandHandler(config.operator_id, config.command_prefix)
        self.command_wrapper = TelegramCommandWrapper(self.client, self.commands)
        self.vc = VoiceChatController(self.client)
        self.bridge: RawAudioBridge | None = None
        self.arbiter: AudioOutputArbiter | None = None
        self.ai: AiVoiceProducer | None = None
        self.music: MusicController | None = None
        self.membership: MembershipWatcher | None = None
        self.activity: ActivityTracker | None = None
        self._activity_ready = False
        self._operator_peer: Any | None = None
        self._media_chat_id: int | None = None
        self._closing_call = False
        self._media_lock = asyncio.Lock()
        self._shutting_down = False
        self._activity_jobs: set[asyncio.Task[None]] = set()
        self._register_commands()

    def _register_commands(self) -> None:
        for name in (
            "join",
            "leave",
            "start",
            "stop",
            "status",
            "vc",
            "vcstart",
            "vcstop",
            "play",
            "skip",
            "pause",
            "resume",
            "queue",
        ):
            self.commands.register(name, getattr(self, f"_cmd_{name}"))

    async def _cmd_join(self, command: ParsedCommand) -> str:
        target = command.join_target()
        if target is None:
            return fmt.error("Use /join in a group/channel, or /join <chat> in private messages.")
        async with self._media_lock:
            try:
                entity = await self.vc.resolve(target)
                status = await self.vc.status(entity)
            except VoiceChatError as exc:
                return fmt.error(str(exc))
            if not status.active:
                return fmt.error("No active voice chat in that group/channel.")
            chat_id = get_peer_id(entity)
            if self._media_chat_id == chat_id and self.bridge is not None:
                return fmt.success("Parlay is already connected to this voice chat.")
            if self.sessions.active:
                return fmt.error(
                    "Parlay is connected to another chat. Leave it before joining here."
                )
            if self.activity is not None and self._activity_ready:
                await self.activity.reconcile(chat_id)
            self.sessions.begin_join(str(chat_id))
            self._media_chat_id = chat_id

            async def dropped() -> None:
                await self._queue_activity_unavailable(chat_id, "transport_disconnected")

            bridge = RawAudioBridge(self.client, on_disconnect=dropped)
            try:
                await bridge.start(entity)
                self.bridge = bridge
                self.arbiter = AudioOutputArbiter(bridge)
                self.music = self._build_music(self.arbiter)
                self.sessions.mark_connected()
                if self.activity is not None and self._activity_ready:
                    await self.activity.set_transport(chat_id, True)
            except asyncio.CancelledError:
                await self._close_call()
                await bridge.stop()
                raise
            except Exception:
                log.exception("Could not connect media transport for chat %s", chat_id)
                await self._close_call()
                # start can fail before the bridge becomes the active bridge.
                if self.bridge is not bridge:
                    try:
                        await bridge.stop()
                    except Exception:
                        log.warning("Failed to clean up incomplete media join", exc_info=True)
                return fmt.error(
                    "Could not connect Parlay's audio transport. Check the service log."
                )
            return fmt.success(f"Connected Parlay to {getattr(entity, 'title', chat_id)}.")

    def _build_music(self, arbiter: AudioOutputArbiter) -> MusicController:
        selector = SourceSelector(PoTokenProvider(self.config.pot_provider_url))
        return MusicController(
            self.sessions,
            arbiter,
            TrackResolver(selector),
            MediaTranscoder(),
            post_message=self._post_to_call,
        )

    def _ai_configuration(self) -> SessionConfiguration:
        default = default_configuration()
        return SessionConfiguration(
            model=self.config.gemini_model or default.model,
            system_instruction=self.config.gemini_persona or default.system_instruction,
            voice=self.config.gemini_voice or default.voice,
            response_modality=default.response_modality,
        )

    async def _post_to_call(self, text: str) -> None:
        if self._media_chat_id is None:
            return
        try:
            await self.vc.send_message(self._media_chat_id, text)
        except VoiceChatError as exc:
            log.warning("In-call message suppressed: %s", exc)

    async def _notify_operator(self, text: str) -> None:
        target = self._operator_peer if self._operator_peer is not None else self.config.operator_id
        await self.client.send_message(target, text)

    async def _on_ai_speaking(self) -> None:
        await self._post_to_call(fmt.status("The AI is speaking.", "speaking"))

    def _wrong_chat(self, command: ParsedCommand) -> bool:
        return bool(
            (command.is_group or command.is_channel)
            and command.chat_id != self._media_chat_id
            and self._media_chat_id is not None
        )

    async def _missing_media(self, command: ParsedCommand) -> str:
        detail = "Parlay's audio transport is not connected."
        if (
            self.activity is not None
            and self._activity_ready
            and command.chat_id is not None
            and (command.is_group or command.is_channel)
        ):
            await self.activity.reconcile(command.chat_id)
            detail = await self.activity.status_text(command.chat_id)
        return fmt.error(f"{detail} Use /join here to connect Parlay's audio transport.")

    async def _cmd_leave(self, command: ParsedCommand) -> str:
        async with self._media_lock:
            if self._wrong_chat(command):
                return fmt.error("Parlay is connected to a different chat.")
            if not self.sessions.active:
                return await self._missing_media(command)
            await self._close_call()
        return fmt.success("Left the voice chat.")

    async def _cmd_start(self, command: ParsedCommand) -> str:
        if self._wrong_chat(command):
            return fmt.error("Parlay is connected to a different chat.")
        if self.bridge is None or self.arbiter is None:
            return await self._missing_media(command)
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
        except ProviderError:
            self.sessions.disengage_ai()
            log.exception("Failed to engage AI voice pipeline")
            return fmt.error("Could not engage the AI pipeline. Check the service log.")
        self.ai = ai
        return fmt.success("AI voice pipeline engaged.")

    async def _cmd_stop(self, command: ParsedCommand) -> str:
        if self._wrong_chat(command):
            return fmt.error("Parlay is connected to a different chat.")
        if self.music is not None and self.music.is_active:
            return await self.music.stop()
        try:
            self.sessions.disengage_ai()
        except SessionError as exc:
            return fmt.error(str(exc))
        await self._teardown_pipeline()
        return fmt.success("AI voice pipeline stopped.")

    async def _cmd_status(self, command: ParsedCommand) -> str:
        target = command.join_target()
        if target is None:
            target = self._media_chat_id
        if target is not None and self.activity is not None and self._activity_ready:
            try:
                entity = await self.vc.resolve(target)
                chat_id = get_peer_id(entity)
                # get_active_call also verifies the entity is a group/channel.
                await self.vc.get_active_call(entity)
                await self.activity.reconcile(chat_id)
                return fmt.status(await self.activity.status_text(chat_id), "idle")
            except VoiceChatError as exc:
                return fmt.error(str(exc))
        return fmt.status(self.sessions.status_text(), "connected" if self.bridge else "idle")

    def _vc_target(self, command: ParsedCommand) -> Any | None:
        target = command.join_target()
        return target if target is not None else self._media_chat_id

    async def _cmd_vc(self, command: ParsedCommand) -> str:
        target = self._vc_target(command)
        if target is None:
            return fmt.error("Use /vc in a group/channel or /vc <chat>.")
        try:
            status = await self.vc.status(target)
        except VoiceChatError as exc:
            return fmt.error(str(exc))
        if not status.active:
            return fmt.status("No active voice chat.", "idle")
        return fmt.status(
            f"Voice chat is live with {status.participants} participant(s).", "connected"
        )

    async def _cmd_vcstart(self, command: ParsedCommand) -> str:
        target = self._vc_target(command)
        if target is None:
            return fmt.error("Use /vcstart in a group/channel or /vcstart <chat>.")
        try:
            await self.vc.start(target)
        except VoiceChatError as exc:
            return fmt.error(str(exc))
        return fmt.success("Voice chat started.")

    async def _cmd_vcstop(self, command: ParsedCommand) -> str:
        target = self._vc_target(command)
        if target is None:
            return fmt.error("Use /vcstop in a group/channel or /vcstop <chat>.")
        try:
            await self.vc.stop(target)
        except VoiceChatError as exc:
            return fmt.error(str(exc))
        return fmt.success("Voice chat stopped.")

    async def _music_command(self, command: ParsedCommand, method: str) -> str:
        if self._wrong_chat(command):
            return fmt.error("Parlay is connected to a different chat.")
        if self.music is None:
            return await self._missing_media(command)
        if method == "play":
            return await self.music.play(command.args)
        result: str = await getattr(self.music, method)()
        return result

    async def _cmd_play(self, command: ParsedCommand) -> str:
        return await self._music_command(command, "play")

    async def _cmd_skip(self, command: ParsedCommand) -> str:
        return await self._music_command(command, "skip")

    async def _cmd_pause(self, command: ParsedCommand) -> str:
        return await self._music_command(command, "pause")

    async def _cmd_resume(self, command: ParsedCommand) -> str:
        return await self._music_command(command, "resume")

    async def _cmd_queue(self, command: ParsedCommand) -> str:
        return await self._music_command(command, "show_queue")

    async def _teardown_pipeline(self) -> None:
        ai, self.ai = self.ai, None
        if ai is not None:
            await ai.disengage()

    async def _teardown_bridge(self) -> None:
        music, self.music = self.music, None
        bridge, self.bridge = self.bridge, None
        self.arbiter = None
        try:
            if music is not None:
                await music.on_session_end()
        finally:
            if bridge is not None:
                await bridge.stop()

    async def _close_call(self) -> None:
        if self._closing_call:
            return
        self._closing_call = True
        chat_id, self._media_chat_id = self._media_chat_id, None
        try:
            try:
                await self._teardown_pipeline()
            finally:
                await self._teardown_bridge()
        finally:
            if self.sessions.active:
                self.sessions.end()
            try:
                if chat_id is not None and self.activity is not None and self._activity_ready:
                    await self.activity.set_transport(chat_id, False)
            finally:
                self._closing_call = False

    async def _queue_activity_unavailable(self, chat_id: int, reason: str) -> None:
        # Never wait for a media operation from a Telegram update handler.
        # Capture the session object to reject delayed events after a rejoin.
        session = self.sessions.session
        if self._shutting_down or session is None or chat_id != self._media_chat_id:
            return

        async def apply() -> None:
            if self.sessions.session is not session:
                return
            try:
                await self._on_activity_unavailable(chat_id, reason, expected_session=session)
            except Exception:
                log.exception("Activity cleanup failed for chat %s", chat_id)

        task = asyncio.create_task(apply(), name="parlay-activity-cleanup")
        self._activity_jobs.add(task)
        task.add_done_callback(self._activity_jobs.discard)

    async def _on_activity_unavailable(
        self,
        chat_id: int,
        reason: str,
        *,
        expected_session: Any = None,
    ) -> None:
        if self._closing_call or self._shutting_down or chat_id != self._media_chat_id:
            return
        async with self._media_lock:
            if chat_id != self._media_chat_id:
                return
            if expected_session is not None and self.sessions.session is not expected_session:
                return
            if reason == "media_revoked":
                # Admin mute retains the receive connection.
                if self.music is not None and self.music.is_active:
                    await self.music.pause()
                await self._teardown_pipeline()
                if self.sessions.active:
                    self.sessions.disengage_ai()
                await self._notify_operator(
                    "Parlay was muted by an admin; outgoing playback is paused."
                )
                return
            await self._close_call()
        try:
            await self._notify_operator(f"Parlay's call in {chat_id} ended ({reason}).")
        except Exception:
            log.warning("Cannot notify operator about ended call", exc_info=True)

    async def _on_call_dropped(self) -> None:
        if self._media_chat_id is not None:
            await self._on_activity_unavailable(self._media_chat_id, "transport_disconnected")

    async def _on_pipeline_loss(self, reason: str) -> None:
        self.ai = None
        if self.sessions.active:
            self.sessions.disengage_ai()
        await self._post_to_call(fmt.error(f"The AI encountered an error: {reason}"))

    async def _on_message(self, event: Any) -> None:
        await self.command_wrapper.handle(event)

    async def _on_raw_update(self, update: Any) -> None:
        if self.activity is not None and self._activity_ready and not self._shutting_down:
            try:
                await self.activity.handle_update(update)
            except Exception:
                log.exception("Failed to process Telegram activity update")

    async def _on_chat_action(self, event: Any) -> None:
        await self._on_raw_update(event)
        if self.membership is not None:
            try:
                await self.membership.handle(event)
            except Exception:
                log.exception("Failed to audit membership update")

    def _build_membership(self, self_id: str) -> MembershipWatcher:
        notifier = MembershipNotifier(self._notify_operator)
        return MembershipWatcher(self_id, [notifier, AuditLogStore(self.config.audit_log_path)])

    async def _resolve_operator(self) -> Any | None:
        try:
            entity = await self.client.get_entity(self.config.operator_id)
        except (ValueError, TypeError):
            log.warning("Could not resolve operator; retaining configured identity gate")
            return None
        self._operator_peer = await self.client.get_input_entity(entity)
        self.commands.set_operator_id(str(entity.id))
        return entity

    async def run(self) -> None:
        await start_authorized(self.client)
        try:
            me = await self.client.get_me()
            await self._resolve_operator()
            self.commands.set_account(me.id, getattr(me, "username", None))
            self.membership = self._build_membership(str(me.id))
            self.activity = ActivityTracker(
                self.client,
                self.config.activity_db_path,
                me.id,
                self._queue_activity_unavailable,
            )
            await self.activity.start()
            self._activity_ready = True
            self.client.add_event_handler(self._on_raw_update, events.Raw())
            self.client.add_event_handler(self._on_chat_action, events.ChatAction())
            self.command_wrapper.install()
            log.info("Parlay is online; command and activity listeners registered")
            await self.client.run_until_disconnected()
        finally:
            self._shutting_down = True
            for task in tuple(self._activity_jobs):
                task.cancel()
            await asyncio.gather(*self._activity_jobs, return_exceptions=True)
            self.command_wrapper.uninstall()
            self.client.remove_event_handler(self._on_raw_update)
            self.client.remove_event_handler(self._on_chat_action)
            try:
                await self._close_call()
            finally:
                self._activity_ready = False
                if self.activity is not None:
                    await self.activity.stop()
                await self.client.disconnect()
