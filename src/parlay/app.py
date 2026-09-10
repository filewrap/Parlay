"""Linux orchestration for Telegram media, shared rooms, and Compass."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
import uuid
from typing import Any

from telethon import events
from telethon.tl import functions, types
from telethon.utils import get_peer_id

from . import presentation as fmt
from .activity import ActivityTracker
from .bot import CompanionBot
from .client import build_client, start_authorized
from .command_wrapper import TelegramCommandWrapper
from .commands import CommandHandler, ParsedCommand
from .config import Config
from .membership.notifier import MembershipNotifier
from .membership.store import AuditLogStore
from .membership.watcher import MembershipWatcher
from .ml import CompassService
from .rooms.gateway import create_app
from .rooms.service import RoomError, RoomService
from .runtime import Runtime, RuntimeRegistry
from .search import MediaSearch
from .vc import VoiceChatController, VoiceChatError
from .voice.ai_producer import AiVoiceProducer
from .voice.gemini import GeminiVoiceProvider, default_configuration
from .voice.provider import SessionConfiguration

log = logging.getLogger(__name__)


class ParlayApp:
    """One user-account transport, separate bot identity, and per-chat runtimes."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.client = build_client(config)
        self.vc = VoiceChatController(self.client)
        self.commands = CommandHandler(config.operator_id, config.command_prefix)
        self.command_wrapper = TelegramCommandWrapper(self.client, self.commands)
        self.registry = RuntimeRegistry(
            self.client,
            config,
            self._on_playback,
            self._on_closed,
            self._on_transport,
        )
        self.search = MediaSearch()
        self.activity: ActivityTracker | None = None
        self.rooms: RoomService | None = None
        self.compass: CompassService | None = None
        self.bot: CompanionBot | None = None
        self.membership: MembershipWatcher | None = None
        self._activity_ready = False
        self._operator_peer: Any = None
        self._account_id: int | None = None
        self._chat_locks: dict[int, asyncio.Lock] = {}
        self._jobs: set[asyncio.Task[Any]] = set()
        self._recoveries: dict[int, asyncio.Task[Any]] = {}
        self._last_playback: dict[int, dict[str, Any]] = {}
        self._room_owners: dict[int, int] = {}
        self._shutting_down = False
        self._server: Any = None
        self._people: dict[tuple[int, int], tuple[float, Any]] = {}
        self._people_locks: dict[tuple[int, int], asyncio.Lock] = {}
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

    def _spawn(self, awaitable: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(awaitable)
        self._jobs.add(task)

        def done(finished: asyncio.Task[Any]) -> None:
            self._jobs.discard(finished)
            if not finished.cancelled() and finished.exception() is not None:
                log.error("Background operation failed", exc_info=finished.exception())

        task.add_done_callback(done)
        return task

    async def _resolve_target(self, command: ParsedCommand) -> int:
        target = command.join_target()
        if target is None:
            raise VoiceChatError("Use this command in a group/channel or specify a chat.")
        entity = await self.vc.resolve(target)
        if not isinstance(entity, (types.Chat, types.Channel)):
            raise VoiceChatError("Voice chats exist only in groups and channels.")
        return int(get_peer_id(entity))

    async def _join_chat(self, chat_id: int) -> Runtime:
        lock = self._chat_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            call = await self.vc.get_active_call(chat_id)
            if call is None or not (await self.vc.status(chat_id)).active:
                raise VoiceChatError("No active voice chat in this group/channel.")
            existing = self.registry.get(chat_id)
            if existing is not None and existing.call_id not in (None, call.id):
                await self.registry.leave(chat_id, "call_replaced")
            runtime = await self.registry.join(chat_id)
            runtime.bind_call_id(int(call.id))
            if self.activity is not None and self._activity_ready:
                await self.activity.reconcile(chat_id)
                await self.activity.set_transport(chat_id, True)
            return runtime

    async def _cmd_join(self, command: ParsedCommand) -> str:
        try:
            chat_id = await self._resolve_target(command)
            existing = self.registry.get(chat_id)
            await self._join_chat(chat_id)
            return fmt.success(
                "Parlay is already connected here."
                if existing
                else "Connected Parlay to this voice chat."
            )
        except (VoiceChatError, RuntimeError, ValueError) as exc:
            return fmt.error(str(exc))

    async def _cmd_leave(self, command: ParsedCommand) -> str:
        chat_id = await self._resolve_target(command)
        recovery = self._recoveries.pop(chat_id, None)
        if recovery is not None:
            recovery.cancel()
        await self.registry.leave(chat_id, "left")
        return fmt.success("Parlay left this voice chat.")

    async def _cmd_status(self, command: ParsedCommand) -> str:
        chat_id = await self._resolve_target(command)
        if self.activity is not None:
            await self.activity.reconcile(chat_id)
            return fmt.status(await self.activity.status_text(chat_id), "idle")
        return fmt.status("Activity tracking is starting.", "idle")

    async def _cmd_vc(self, command: ParsedCommand) -> str:
        status = await self.vc.status(await self._resolve_target(command))
        return fmt.status(
            f"Voice chat active: {status.participants} participants."
            if status.active
            else "No active voice chat.",
            "idle",
        )

    async def _cmd_vcstart(self, command: ParsedCommand) -> str:
        await self.vc.start(await self._resolve_target(command))
        return fmt.success("Voice chat started.")

    async def _cmd_vcstop(self, command: ParsedCommand) -> str:
        chat_id = await self._resolve_target(command)
        await self.vc.stop(chat_id)
        await self.registry.leave(chat_id, "call_discarded")
        if self.rooms is not None:
            await self.rooms.end_group(chat_id, "call_discarded")
        return fmt.success("Voice chat stopped.")

    async def _cmd_play(self, command: ParsedCommand) -> str:
        if not command.args:
            return fmt.error("Give a song name or YouTube link.")
        if command.chat_id is None or not (command.is_group or command.is_channel):
            return fmt.error("Use /play in the group/channel where music should play.")
        room = await self._play_for_user(int(command.sender_id), command.chat_id, command.args)
        track = room.get("playback", {}).get("track")
        message = f"Playback updated: {track['title']}" if track else "Playback updated."
        if self.bot is not None and room.get("id"):
            message += f"\nhttps://t.me/{self.bot.username}?startapp={room['id']}"
        return fmt.success(message)

    async def _play_for_user(self, user_id: int, chat_id: int, query: str) -> dict[str, Any]:
        if not await self._authority(user_id, chat_id):
            raise RoomError("control_forbidden", "Group owner or Parlay operator required", 403)
        tracks = await self.search(query)
        if not tracks:
            raise RoomError("track_not_found", "No playable track found", 404)
        track = tracks[0]
        runtime = await self._join_chat(chat_id)
        room = None
        if self.rooms is not None:
            if runtime.call_id is None:
                raise RuntimeError("Telegram call identifier is missing")
            self._room_owners[chat_id] = user_id
            room = await self.rooms.ensure_group(chat_id, runtime.call_id, user_id)
        snapshot = await self.registry.command(chat_id, "play", track["source_url"])
        if self.compass is not None:
            await asyncio.to_thread(
                self.compass.ingest_track,
                track["id"],
                track["title"],
                track.get("artist", ""),
                track["source_url"],
            )
            await asyncio.to_thread(
                self.compass.record_event,
                str(user_id),
                track["id"],
                "play",
                uuid.uuid4().hex,
                {"chat_id": chat_id, "origin": "telegram_request"},
            )
        if self.rooms is not None and room is not None:
            await self.rooms.publish_playback(chat_id, snapshot)
            return await self.rooms.snapshot(room["id"], user_id)
        return {"playback": snapshot}

    async def _room_action_event(
        self,
        room_id: str,
        user_id: int,
        action: str,
        track: dict[str, Any],
        event_id: str,
    ) -> None:
        if self.compass is None:
            return
        await asyncio.to_thread(
            self.compass.ingest_track,
            track["id"],
            track["title"],
            "",
            track["source_url"],
        )
        await asyncio.to_thread(
            self.compass.record_event,
            str(user_id),
            track["id"],
            "play",
            event_id,
            {"room_id": room_id, "origin": action},
        )

    async def _room_playback(
        self, chat_id: int, action: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        # RoomService has already checked actor permissions and owns its action lock.
        # Do not publish back into RoomService while that lock is held.
        if self.registry.get(chat_id) is None:
            raise RoomError(
                "recovering", "Voice connection is not ready. Retry after recovery.", 409
            )
        if action in {"queue_add", "force_play"}:
            track = payload["track"]
            return await self.registry.command(
                chat_id, "play" if action == "queue_add" else "force_play", track["source_url"]
            )
        return await self.registry.command(chat_id, action, {})

    async def _music_action(self, command: ParsedCommand, action: str) -> str:
        if command.chat_id is None or not (command.is_group or command.is_channel):
            return fmt.error("Use music controls in the target group/channel.")
        runtime = self.registry.get(command.chat_id)
        if runtime is None:
            return fmt.error("Parlay's audio transport is not connected here. Use /join.")
        snapshot = await self.registry.command(command.chat_id, action)
        if action == "queue":
            titles = [item["title"] for item in snapshot["queue"]]
            return fmt.status(
                "Queue:\n" + "\n".join(titles) if titles else "Queue is empty.", "queue"
            )
        return fmt.success(f"Playback {action} applied in this chat.")

    async def _cmd_skip(self, command: ParsedCommand) -> str:
        return await self._music_action(command, "skip")

    async def _cmd_pause(self, command: ParsedCommand) -> str:
        return await self._music_action(command, "pause")

    async def _cmd_resume(self, command: ParsedCommand) -> str:
        return await self._music_action(command, "resume")

    async def _cmd_queue(self, command: ParsedCommand) -> str:
        return await self._music_action(command, "queue")

    async def _cmd_stop(self, command: ParsedCommand) -> str:
        runtime = self.registry.get(command.chat_id or 0)
        if runtime is not None and runtime.music.is_active:
            return await self._music_action(command, "stop")
        if runtime is not None and runtime.ai is not None:
            await runtime.ai.disengage()
            runtime.ai = None
            runtime.sessions.disengage_ai()
            return fmt.success("AI stopped in this chat.")
        return fmt.status("Nothing is playing here.", "idle")

    async def _cmd_start(self, command: ParsedCommand) -> str:
        if not self.config.gemini_api_key:
            return fmt.error("AI voice is disabled. Configure GEMINI_API_KEY to enable it.")
        runtime = self.registry.get(command.chat_id or 0)
        if runtime is None:
            return fmt.error("Connect Parlay here with /join before starting AI voice.")
        if runtime.ai is not None:
            return fmt.warning("AI is already running here.")
        runtime.sessions.engage_ai()
        default = default_configuration()
        config = SessionConfiguration(
            model=self.config.gemini_model or default.model,
            system_instruction=self.config.gemini_persona or default.system_instruction,
            voice=self.config.gemini_voice or default.voice,
            response_modality=default.response_modality,
        )

        async def lost(reason: str) -> None:
            if self.registry.get(runtime.chat_id) is runtime:
                runtime.ai = None
                if runtime.sessions.active:
                    runtime.sessions.disengage_ai()
                await self._notify_operator("AI voice stopped after a provider failure.")

        ai = AiVoiceProducer(
            GeminiVoiceProvider(self.config.gemini_api_key, config),
            source=runtime.bridge,
            arbiter=runtime.arbiter,
            on_loss=lost,
        )
        try:
            await ai.engage()
        except BaseException:
            runtime.sessions.disengage_ai()
            raise
        runtime.ai = ai
        return fmt.success("AI voice started in this chat.")

    async def _participant(self, user_id: int, chat_id: int) -> Any:
        key = (chat_id, user_id)
        lock = self._people_locks.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._people.get(key)
            if cached and cached[0] > time.monotonic():
                return cached[1]
            person = await self._fetch_participant(user_id, chat_id)
            if len(self._people) >= 4096:
                self._people.pop(next(iter(self._people)))
            self._people[key] = (time.monotonic() + (15 if person is not None else 3), person)
            return person

    async def _fetch_participant(self, user_id: int, chat_id: int) -> Any:
        # Use each client's own entity cache. Access hashes are not portable.
        candidates = [self.client]
        if self.bot is not None and self.bot.client is not None:
            candidates.insert(0, self.bot.client)
        for client in candidates:
            try:
                entity = await client.get_entity(chat_id)
                if isinstance(entity, types.Channel):
                    result = await client(functions.channels.GetParticipantRequest(entity, user_id))
                    return result.participant
                if isinstance(entity, types.Chat):
                    result = await client(functions.messages.GetFullChatRequest(entity.id))
                    people = getattr(result.full_chat.participants, "participants", [])
                    return next((p for p in people if p.user_id == user_id), None)
            except Exception:
                continue
        return None

    async def _member(self, user_id: int, chat_id: int) -> bool:
        person = await self._participant(user_id, chat_id)
        if person is None or isinstance(person, types.ChannelParticipantLeft):
            return False
        if isinstance(person, types.ChannelParticipantBanned):
            return not bool(person.left or person.banned_rights.view_messages)
        return True

    async def _authority(self, user_id: int, chat_id: int) -> bool:
        if self.commands.is_operator(user_id):
            return True
        person = await self._participant(user_id, chat_id)
        return isinstance(person, (types.ChannelParticipantCreator, types.ChatParticipantCreator))

    async def _on_playback(self, chat_id: int, snapshot: dict[str, Any]) -> None:
        self._last_playback[chat_id] = snapshot
        if self.rooms is not None and not self._shutting_down:
            await self.rooms.publish_playback(chat_id, snapshot)

    async def _on_transport(self, chat_id: int, connected: bool) -> None:
        if self.activity is not None and self._activity_ready:
            actual = self.registry.get(chat_id) is not None
            await self.activity.set_transport(chat_id, actual)

    async def _on_closed(self, chat_id: int, reason: str) -> None:
        if self._shutting_down or self.registry.get(chat_id) is not None:
            return
        if reason == "transport_disconnected" and chat_id in self._room_owners:
            if self.rooms is not None:
                await self.rooms.set_recovering(chat_id, reason)
            if chat_id not in self._recoveries:
                task = self._spawn(self._recover(chat_id))
                self._recoveries[chat_id] = task
            return
        if self.rooms is not None:
            await self.rooms.end_group(chat_id, reason)

    async def _recover(self, chat_id: int) -> None:
        try:
            for delay in (2, 5, 10):
                await asyncio.sleep(delay)
                if self._shutting_down:
                    return
                try:
                    runtime = await self._join_chat(chat_id)
                    owner = self._room_owners[chat_id]
                    if self.rooms is not None and runtime.call_id is not None:
                        await self.rooms.ensure_group(chat_id, runtime.call_id, owner)
                        # Reconnect never silently replays a stale track from its beginning.
                        await self.rooms.publish_playback(chat_id, runtime.music.snapshot())
                    await self._notify_operator(
                        f"Voice connection recovered in {chat_id}; playback is idle."
                    )
                    return
                except Exception:
                    log.warning("Call recovery attempt failed for %s", chat_id)
            if self.rooms is not None:
                await self.rooms.end_group(chat_id, "terminal_connection_failure")
        finally:
            self._recoveries.pop(chat_id, None)

    async def _queue_activity_unavailable(self, chat_id: int, reason: str) -> None:
        runtime = self.registry.get(chat_id)

        async def apply() -> None:
            if runtime is not None and self.registry.get(chat_id) is not runtime:
                return
            if reason == "media_revoked" and runtime is not None:
                await runtime.command("pause")
                if runtime.ai is not None:
                    await runtime.ai.disengage()
                    runtime.ai = None
                    if runtime.sessions.active:
                        runtime.sessions.disengage_ai()
                return
            recovery = self._recoveries.pop(chat_id, None)
            if recovery is not None:
                recovery.cancel()
            if runtime is not None:
                await self.registry.leave(chat_id, reason)
            if self.rooms is not None:
                await self.rooms.end_group(chat_id, reason)

        if not self._shutting_down:
            self._spawn(apply())

    async def _on_raw_update(self, update: Any) -> None:
        if isinstance(update, types.UpdateChannel):
            chat_id = get_peer_id(types.PeerChannel(update.channel_id))
            for key in tuple(self._people):
                if key[0] == chat_id:
                    self._people.pop(key, None)
        if type(update).__name__ == "UpdateChannelParticipant":
            chat_id = get_peer_id(types.PeerChannel(update.channel_id))
            self._people.pop((chat_id, update.user_id), None)
        if self.activity is not None and self._activity_ready and not self._shutting_down:
            try:
                await self.activity.handle_update(update)
            except Exception:
                log.exception("Activity update failed")

    async def _on_chat_action(self, event: Any) -> None:
        for key in tuple(self._people):
            if key[0] == event.chat_id:
                self._people.pop(key, None)
        await self._on_raw_update(event)
        if self.membership is not None:
            await self.membership.handle(event)

    async def _notify_operator(self, text: str) -> None:
        await self.client.send_message(self._operator_peer or self.config.operator_id, text)

    async def _reconcile_stored_rooms(self) -> None:
        if self.rooms is None:
            return

        db_path = self.rooms.db_path

        def active_rooms() -> list[Any]:
            with sqlite3.connect(db_path) as db:
                return db.execute(
                    "SELECT chat_id,call_id,owner_id FROM rooms WHERE kind='group' AND state!='ended'"
                ).fetchall()

        for chat_id, call_id, owner_id in await asyncio.to_thread(active_rooms):
            self._room_owners[chat_id] = owner_id
            try:
                call = await self.vc.get_active_call(chat_id)
                if call is None or call.id != call_id:
                    await self.rooms.end_group(chat_id, "call_ended_while_offline")
                else:
                    await self.rooms.set_recovering(chat_id, "backend_restarted")
                    self._recoveries[chat_id] = self._spawn(self._recover(chat_id))
            except Exception:
                await self.rooms.set_recovering(chat_id, "verification_unavailable")
                self._recoveries[chat_id] = self._spawn(self._recover(chat_id))

    async def run(self) -> None:
        await start_authorized(self.client)
        server_task = None
        try:
            me = await self.client.get_me()
            self._account_id = me.id
            self.commands.set_account(me.id, getattr(me, "username", None))
            operator = await self.client.get_entity(self.config.operator_id)
            self.commands.set_operator_id(str(operator.id))
            self._operator_peer = await self.client.get_input_entity(operator)
            self.activity = ActivityTracker(
                self.client, self.config.activity_db_path, me.id, self._queue_activity_unavailable
            )
            await self.activity.start()
            self._activity_ready = True
            self.membership = MembershipWatcher(
                str(me.id),
                [
                    MembershipNotifier(self._notify_operator),
                    AuditLogStore(self.config.audit_log_path),
                ],
            )
            self.client.add_event_handler(self._on_raw_update, events.Raw())
            self.client.add_event_handler(self._on_chat_action, events.ChatAction())
            self.command_wrapper.install()
            self.compass = await asyncio.to_thread(
                CompassService,
                self.config.ml_db_path,
                self.config.ml_model_dir,
                self.config.youtube_api_key,
            )
            if self.config.bot_token:
                self.rooms = await asyncio.to_thread(
                    RoomService,
                    self.config.room_db_path,
                    self._room_playback,
                    authority=self._authority,
                    member=self._member,
                    search=self.search,
                    on_action=self._room_action_event,
                )
                await self.rooms.start()
                self.bot = CompanionBot(
                    self.config, self.rooms, self.compass, self._play_for_user, self._authority
                )
                await self.bot.start()
                self.rooms.on_reentry = self.bot.notify_reentry
                await self.compass.start(deliver=self.bot.deliver)
                import uvicorn

                gateway = create_app(
                    self.rooms,
                    self.config.bot_token,
                    list(self.config.allowed_origins),
                    self.compass,
                    self.search,
                )
                self._server = uvicorn.Server(
                    uvicorn.Config(
                        gateway,
                        host=self.config.backend_host,
                        port=self.config.backend_port,
                        access_log=False,
                        ws_max_size=65_536,
                        workers=1,
                    )
                )
                server_task = asyncio.create_task(self._server.serve(), name="rooms-asgi")
                self._spawn(self._reconcile_stored_rooms())
            else:
                log.warning("BOT_TOKEN is absent: companion bot and room gateway are disabled")
                await self.compass.start()
            waiters = [asyncio.create_task(self.client.run_until_disconnected())]
            if server_task is not None:
                waiters.append(server_task)
            if self.bot is not None and self.bot.client is not None:
                waiters.append(asyncio.create_task(self.bot.client.run_until_disconnected()))
            try:
                done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                if self._server is not None:
                    self._server.should_exit = True
                for task in waiters:
                    if task is not server_task:
                        task.cancel()
                await asyncio.gather(*waiters, return_exceptions=True)
        finally:
            self._shutting_down = True
            self.command_wrapper.uninstall()
            self.client.remove_event_handler(self._on_raw_update)
            self.client.remove_event_handler(self._on_chat_action)
            for task in tuple(self._jobs):
                task.cancel()
            await asyncio.gather(*tuple(self._jobs), return_exceptions=True)
            try:
                await self.registry.close()
            finally:
                if self.compass is not None:
                    await self.compass.stop()
                if self.bot is not None:
                    await self.bot.stop()
                if self.rooms is not None:
                    await self.rooms.stop()
                self._activity_ready = False
                if self.activity is not None:
                    await self.activity.stop()
                await self.client.disconnect()
