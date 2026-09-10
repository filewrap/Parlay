"""Independent, concurrent per-chat media runtimes.

The registry is intentionally not wired into ParlayApp yet. It owns capacity,
per-chat lifecycle serialization, generation checks, and callback isolation.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import socket
import sqlite3
import uuid
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


class RuntimeCapacityError(RuntimeError):
    """Raised when all configured concurrent call slots are reserved."""


class UnsafeMediaSourceError(ValueError):
    """Raised when a direct URL resolves to a local or special-use address."""


class Runtime:
    """All media state for one chat and one registry generation."""

    def __init__(
        self,
        *,
        chat_id: int,
        generation: int,
        call_id: str,
        sessions: CallSessionManager,
        bridge: RawAudioBridge,
        arbiter: AudioOutputArbiter,
        music: MusicController,
    ) -> None:
        self.chat_id = chat_id
        self.generation = generation
        self.call_id = call_id
        self.sessions = sessions
        self.bridge = bridge
        self.arbiter = arbiter
        self.music = music
        self.ai: Any | None = None
        self._command_lock = asyncio.Lock()
        self._closed = False

    async def command(self, action: str, payload: Any = None) -> Snapshot:
        async with self._command_lock:
            if self._closed:
                raise RuntimeError("runtime is closed")
            if action == "play":
                for request in _requests(payload):
                    await _reject_private_url(request)
                    await self.music.play(request)
            elif action == "force_play":
                request = _one_request(payload)
                await _reject_private_url(request)
                await self.music.force_play(request)
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
        if self._closed:
            return
        self._closed = True
        if self.ai is not None and getattr(self.ai, "engaged", False):
            await self.ai.disengage()
        await self.music.on_session_end()
        await self.bridge.stop()
        if self.sessions.active:
            self.sessions.end()


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

    def get(self, chat_id: int) -> Runtime | None:
        return self._runtimes.get(chat_id)

    async def join(self, chat: int | Any) -> Runtime:
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
        lock = self._locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            runtime = self._runtimes.get(chat_id)
            if runtime is None:
                return
            generation = runtime.generation
            await runtime._close()
            async with self._state_lock:
                current = self._runtimes.get(chat_id)
                if current is not None and current.generation == generation:
                    self._runtimes.pop(chat_id, None)
                    removed = True
                else:
                    removed = False
        if removed:
            self._notify(self._on_transport, chat_id, False)
            self._notify(self._on_closed, chat_id, reason)

    async def close(self) -> None:
        async with self._state_lock:
            self._closed = True
            chat_ids = list(self._runtimes)
        await asyncio.gather(*(self.leave(chat_id, "registry_closed") for chat_id in chat_ids))

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
            call_id=uuid.uuid4().hex,
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

    @staticmethod
    def _notify(callback: Callable[..., Awaitable[None]] | None, *args: Any) -> None:
        if callback is None:
            return

        async def run() -> None:
            try:
                await callback(*args)
            except Exception:
                log.exception("runtime observer failed")

        asyncio.create_task(run())


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


async def _reject_private_url(request: str) -> None:
    """Reject direct media URLs that resolve to loopback or private networks."""
    parsed = urlsplit(request.strip())
    if parsed.scheme not in {"http", "https"}:
        return
    if not parsed.hostname:
        raise UnsafeMediaSourceError("media URL has no host")
    try:
        addresses = [ipaddress.ip_address(parsed.hostname)]
    except ValueError:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(parsed.hostname, parsed.port, type=socket.SOCK_STREAM)
        addresses = list({ipaddress.ip_address(info[4][0]) for info in infos})
    if not addresses or any(not address.is_global for address in addresses):
        raise UnsafeMediaSourceError("media URL resolves to a non-public address")
