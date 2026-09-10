"""Independent, concurrent per-chat media runtimes.

The parent owns voice-chat discovery and supplies the actual Telegram call ID.
The registry owns bounded capacity, per-chat lifecycle serialization, generation
checks, callback isolation, and media cleanup.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import socket
import sqlite3
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from telethon.utils import get_peer_id

from .audio.arbiter import AudioOutputArbiter
from .audio.bridge import RawAudioBridge
from .media.po_token import PoTokenProvider
from .media.resolver import TrackResolver
from .media.source_selector import SourceSelector
from .media.transcoder import MediaTranscoder
from .music.controller import MusicController, Snapshot
from .session import CallSessionManager

log = logging.getLogger(__name__)
PlaybackCallback = Callable[[int, Snapshot], Awaitable[None]]
ClosedCallback = Callable[[int, str], Awaitable[None]]
TransportCallback = Callable[[int, bool], Awaitable[None]]
_YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
    }
)


class RuntimeCapacityError(RuntimeError):
    """Raised when all configured concurrent call slots are reserved."""


class UnsafeMediaSourceError(ValueError):
    """Raised when a direct URL is outside the public YouTube allowlist."""


class Runtime:
    """All media state for one chat and one registry generation.

    ``call_id`` is the Telegram ``InputGroupCall.id``. It is ``None`` until the
    parent binds the ID obtained during its active-call check. It is never a
    registry-generated correlation ID; ``generation`` provides stale-event
    protection inside the registry.
    """

    def __init__(
        self,
        *,
        chat_id: int,
        generation: int,
        sessions: CallSessionManager,
        bridge: RawAudioBridge,
        arbiter: AudioOutputArbiter,
        music: MusicController,
    ) -> None:
        self.chat_id = chat_id
        self.generation = generation
        self.call_id: int | None = None
        self.sessions = sessions
        self.bridge = bridge
        self.arbiter = arbiter
        self.music = music
        self.ai: Any | None = None
        self._command_lock = asyncio.Lock()
        self._closed = False

    def bind_call_id(self, call_id: int) -> None:
        """Bind the actual Telegram call ID supplied by the parent discovery layer."""
        if isinstance(call_id, bool) or not isinstance(call_id, int) or call_id <= 0:
            raise ValueError("call_id must be a positive Telegram call identifier")
        if self.call_id is not None and self.call_id != call_id:
            raise RuntimeError("runtime is already bound to a different Telegram call")
        self.call_id = call_id

    async def command(self, action: str, payload: Any = None) -> Snapshot:
        async with self._command_lock:
            if self._closed:
                raise RuntimeError("runtime is closed")
            if action == "play":
                for request in _requests(payload):
                    await _validate_media_request(request)
                    await self.music.play_checked(request)
            elif action == "force_play":
                request = _one_request(payload)
                await _validate_media_request(request)
                await self.music.force_play_checked(request)
            elif action == "pause":
                await self.music.pause()
            elif action == "resume":
                await self.music.resume()
            elif action == "skip":
                await self.music.skip()
            elif action == "stop":
                await self.music.stop()
            elif action in {"queue", "snapshot"}:
                pass
            else:
                raise ValueError(f"unknown runtime action: {action}")
            return self.music.snapshot()

    async def _close(self) -> None:
        """Serialize against commands and attempt every cleanup stage exactly once."""
        first_error: BaseException | None = None
        async with self._command_lock:
            if self._closed:
                return
            self._closed = True

            async def cleanup(awaitable: Awaitable[Any], label: str) -> None:
                nonlocal first_error
                try:
                    await awaitable
                except BaseException as exc:
                    log.exception("runtime %s cleanup failed during %s", self.chat_id, label)
                    if first_error is None:
                        first_error = exc

            if self.ai is not None:
                await cleanup(self.ai.disengage(), "AI disengage")
            await cleanup(self.music.on_session_end(), "music stop")
            await cleanup(self.bridge.stop(), "bridge stop")
            if self.sessions.active:
                try:
                    self.sessions.end()
                except BaseException as exc:
                    log.exception("runtime %s cleanup failed during session end", self.chat_id)
                    if first_error is None:
                        first_error = exc

        # Playback observers can re-enter the runtime. Wait only after releasing
        # the command lock, where they fail promptly against the closed state.
        await self.music.wait_for_observer()
        if first_error is not None:
            raise first_error


class _SnapshotStore:
    """Best-effort durable playback metadata. It never stores stream auth URLs."""

    def __init__(self, path: str | None) -> None:
        self._path = path

    async def put(self, chat_id: int, snapshot: Snapshot) -> None:
        if not self._path:
            return
        await asyncio.to_thread(self._put_sync, chat_id, snapshot)

    def _put_sync(self, chat_id: int, snapshot: Snapshot) -> None:
        path = Path(self._path or "")
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS runtime_playback (
                    chat_id INTEGER PRIMARY KEY,
                    snapshot_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )"""
            )
            db.execute(
                """INSERT INTO runtime_playback(chat_id, snapshot_json, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(chat_id) DO UPDATE SET
                     snapshot_json=excluded.snapshot_json,
                     updated_at=excluded.updated_at""",
                (chat_id, json.dumps(snapshot), snapshot["server_time"]),
            )
            db.commit()


class RuntimeRegistry:
    """Create and operate isolated call runtimes with bounded concurrency."""

    def __init__(
        self,
        client: Any,
        config: Any,
        on_playback: PlaybackCallback | None = None,
        on_closed: ClosedCallback | None = None,
        on_transport: TransportCallback | None = None,
    ) -> None:
        self._client = client
        self._config = config
        self._on_playback = on_playback
        self._on_closed = on_closed
        self._on_transport = on_transport
        configured = int(getattr(config, "max_concurrent_calls", 4))
        self._capacity = max(2, configured)
        self._runtimes: dict[int, Runtime] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._generations: dict[int, int] = {}
        self._reserved: set[int] = set()
        self._state_lock = asyncio.Lock()
        self._closed = False
        self._store = _SnapshotStore(getattr(config, "activity_db_path", None))
        self._callback_tasks: set[asyncio.Task[None]] = set()

    def get(self, chat_id: int) -> Runtime | None:
        return self._runtimes.get(chat_id)

    async def join(self, chat: int | Any) -> Runtime:
        """Join media transport; the parent must check that the voice chat is active."""
        if self._closed:
            raise RuntimeError("registry is closed")
        entity, chat_id = await self._resolve(chat)
        lock = self._locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            existing = self._runtimes.get(chat_id)
            if existing is not None:
                return existing
            async with self._state_lock:
                if len(self._runtimes) + len(self._reserved) >= self._capacity:
                    raise RuntimeCapacityError(
                        f"concurrent call capacity reached ({self._capacity})"
                    )
                self._reserved.add(chat_id)
                generation = self._generations.get(chat_id, 0) + 1
                self._generations[chat_id] = generation
            try:
                runtime = await self._build(entity, chat_id, generation)
            except BaseException:
                async with self._state_lock:
                    self._reserved.discard(chat_id)
                raise
            async with self._state_lock:
                self._reserved.discard(chat_id)
                if self._closed:
                    close_now = True
                else:
                    self._runtimes[chat_id] = runtime
                    close_now = False
            if close_now:
                await runtime._close()
                raise RuntimeError("registry closed during join")
        self._notify(self._on_transport, chat_id, True)
        return runtime

    async def command(self, chat_id: int, action: str, payload: Any = None) -> Snapshot:
        runtime = self.get(chat_id)
        if runtime is None:
            if action not in {"play", "force_play"}:
                raise RuntimeError("chat has no media runtime")
            runtime = await self.join(chat_id)
        return await runtime.command(action, payload)

    async def leave(self, chat_id: int, reason: str = "left") -> None:
        cleanup_error: BaseException | None = None
        removed = False
        lock = self._locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            runtime = self._runtimes.get(chat_id)
            if runtime is None:
                return
            generation = runtime.generation
            try:
                await runtime._close()
            except BaseException as exc:
                cleanup_error = exc
            finally:
                async with self._state_lock:
                    current = self._runtimes.get(chat_id)
                    if current is not None and current.generation == generation:
                        self._runtimes.pop(chat_id, None)
                        removed = True
        if removed:
            # Terminal observers are scheduled only after all lifecycle locks are
            # released and after the runtime is absent from the registry.
            self._notify(self._on_transport, chat_id, False)
            self._notify(self._on_closed, chat_id, reason)
        if cleanup_error is not None:
            raise cleanup_error

    async def close(self) -> None:
        async with self._state_lock:
            self._closed = True
            chat_ids = list(self._runtimes)
        results = await asyncio.gather(
            *(self.leave(chat_id, "registry_closed") for chat_id in chat_ids),
            return_exceptions=True,
        )
        await self._wait_for_callbacks()
        errors = [result for result in results if isinstance(result, BaseException)]
        if errors:
            raise ExceptionGroup("one or more runtimes failed to close", errors)

    async def _wait_for_callbacks(self) -> None:
        while self._callback_tasks:
            await asyncio.gather(*tuple(self._callback_tasks), return_exceptions=True)

    async def _build(self, entity: Any, chat_id: int, generation: int) -> Runtime:
        sessions = CallSessionManager()
        sessions.begin_join(str(chat_id))

        async def disconnected() -> None:
            current = self._runtimes.get(chat_id)
            if current is not None and current.generation == generation:
                await self.leave(chat_id, "transport_disconnected")

        bridge = RawAudioBridge(self._client, on_disconnect=disconnected)
        try:
            await bridge.start(entity)
        except BaseException:
            if bridge.active:
                await bridge.stop()
            sessions.end()
            raise
        sessions.mark_connected()
        arbiter = AudioOutputArbiter(bridge)

        async def changed(snapshot: Snapshot) -> None:
            current = self._runtimes.get(chat_id)
            if current is None or current.generation != generation:
                return
            await self._store.put(chat_id, snapshot)
            if self._on_playback is not None:
                await self._on_playback(chat_id, snapshot)

        selector = SourceSelector(PoTokenProvider(getattr(self._config, "pot_provider_url", "")))
        music = MusicController(
            sessions,
            arbiter,
            TrackResolver(selector),
            MediaTranscoder(),
            on_change=changed,
        )
        return Runtime(
            chat_id=chat_id,
            generation=generation,
            sessions=sessions,
            bridge=bridge,
            arbiter=arbiter,
            music=music,
        )

    async def _resolve(self, chat: Any) -> tuple[Any, int]:
        if isinstance(chat, int):
            return chat, chat
        entity = await self._client.get_entity(chat)
        return entity, int(get_peer_id(entity))

    def _notify(self, callback: Callable[..., Awaitable[None]] | None, *args: Any) -> None:
        if callback is None:
            return

        async def run() -> None:
            try:
                await callback(*args)
            except Exception:
                log.exception("runtime observer failed")

        task = asyncio.create_task(run())
        self._callback_tasks.add(task)
        task.add_done_callback(self._callback_tasks.discard)


def _requests(payload: Any) -> Iterable[str]:
    value = payload.get("queue") if isinstance(payload, dict) and "queue" in payload else payload
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)) and value and all(isinstance(item, str) for item in value):
        return tuple(value)
    raise ValueError("play payload must be a request string or a non-empty queue of strings")


def _one_request(payload: Any) -> str:
    value = payload.get("request") if isinstance(payload, dict) else payload
    if not isinstance(value, str) or not value.strip():
        raise ValueError("force_play payload must contain one request string")
    return value


async def _validate_media_request(request: str) -> None:
    """Allow search text and public HTTPS YouTube URLs only."""
    parsed = urlsplit(request.strip())
    if not parsed.scheme and not parsed.netloc:
        return
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or hostname not in _YOUTUBE_HOSTS:
        raise UnsafeMediaSourceError("direct media URLs must use an allowed HTTPS YouTube host")
    try:
        addresses = [ipaddress.ip_address(hostname)]
    except ValueError:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(hostname, parsed.port, type=socket.SOCK_STREAM)
        addresses = list({ipaddress.ip_address(info[4][0]) for info in infos})
    if not addresses or any(not address.is_global for address in addresses):
        raise UnsafeMediaSourceError("media URL resolves to a non-public address")
