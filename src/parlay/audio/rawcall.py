"""Adapter over py-tgcalls 2.3.x and its NTgCalls 2.x backend.

This is the only module that imports pytgcalls. A single PyTgCalls engine is
shared by all calls using the same MTProto client. Each adapter owns one chat,
one update handler, and one paced raw-audio pump.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

log = logging.getLogger(__name__)

SAMPLE_RATE = 48_000
CHANNELS = 2
BYTES_PER_SAMPLE = 2
FRAME_MS = 10
FRAME_BYTES = SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE * FRAME_MS // 1000

RecordedHandler = Callable[[bytes, int], None]
PlayedHandler = Callable[[int], bytes]
DisconnectHandler = Callable[[], None]

# PyTgCalls registers MTProto handlers on its client. Sharing one engine avoids
# duplicate registration. Startup is protected per client, so unrelated clients
# never wait for each other's network I/O.
_apps: dict[int, Any] = {}
_started: set[int] = set()
_start_locks: dict[int, asyncio.Lock] = {}


def _load_api() -> SimpleNamespace:
    """Import the exact py-tgcalls surface used by the raw adapter."""
    from pytgcalls import PyTgCalls
    from pytgcalls.types import (
        AudioQuality,
        ChatUpdate,
        Device,
        Direction,
        ExternalMedia,
        MediaStream,
        RecordStream,
        StreamFrames,
    )

    return SimpleNamespace(
        PyTgCalls=PyTgCalls,
        AudioQuality=AudioQuality,
        ChatUpdate=ChatUpdate,
        Device=Device,
        Direction=Direction,
        ExternalMedia=ExternalMedia,
        MediaStream=MediaStream,
        RecordStream=RecordStream,
        StreamFrames=StreamFrames,
    )


def _get_app(api: SimpleNamespace, client: Any) -> Any:
    key = id(client)
    app = _apps.get(key)
    if app is None:
        app = api.PyTgCalls(client)
        _apps[key] = app
    return app


async def _start_app_once(api: SimpleNamespace, client: Any) -> Any:
    """Start the client's shared engine exactly once under concurrent joins."""
    key = id(client)
    lock = _start_locks.setdefault(key, asyncio.Lock())
    async with lock:
        app = _get_app(api, client)
        if key not in _started:
            await app.start()
            _started.add(key)
        return app


class RawCallAdapter:
    """Join one group call and move 10 ms PCM frames in both directions."""

    def __init__(
        self,
        client: Any,
        on_recorded: RecordedHandler,
        on_played: PlayedHandler,
        on_disconnect: DisconnectHandler | None = None,
    ) -> None:
        self._client = client
        self._on_recorded = on_recorded
        self._on_played = on_played
        self._on_disconnect = on_disconnect
        self._api: SimpleNamespace | None = None
        self._app: Any = None
        self._chat_id: int | None = None
        self._pump_task: asyncio.Task[None] | None = None

    async def start(self, chat: Any) -> None:
        api = self._api = _load_api()
        app = await _start_app_once(api, self._client)
        self._chat_id = await app.resolve_chat_id(chat)
        self._app = app
        app.add_handler(self._handle_update)
        try:
            await app.play(
                self._chat_id,
                api.MediaStream(
                    api.ExternalMedia.AUDIO,
                    audio_parameters=api.AudioQuality.HIGH,
                ),
            )
            await app.record(
                self._chat_id,
                api.RecordStream(audio=True, audio_parameters=api.AudioQuality.HIGH),
            )
        except BaseException:
            app.remove_handler(self._handle_update)
            self._app = None
            self._chat_id = None
            raise
        self._pump_task = asyncio.create_task(self._pump())
        log.info("joined group call %s (external audio in, recording out)", self._chat_id)

    async def stop(self) -> None:
        if self._app is None:
            return
        app, self._app = self._app, None
        if self._pump_task is not None:
            self._pump_task.cancel()
            try:
                await self._pump_task
            except asyncio.CancelledError:
                pass
            self._pump_task = None
        app.remove_handler(self._handle_update)
        chat_id, self._chat_id = self._chat_id, None
        try:
            if chat_id is not None:
                await app.leave_call(chat_id)
        except Exception:
            log.warning("leave_call failed; probably already out of the call", exc_info=True)
        log.info("left group call %s", chat_id)

    async def _pump(self) -> None:
        api = self._api
        assert api is not None
        loop = asyncio.get_running_loop()
        next_at = loop.time()
        while True:
            data = self._on_played(FRAME_BYTES)
            if data and self._app is not None and self._chat_id is not None:
                try:
                    await self._app.send_frame(self._chat_id, api.Device.MICROPHONE, data)
                except Exception:
                    log.debug("send_frame failed", exc_info=True)
            next_at += FRAME_MS / 1000
            delay = next_at - loop.time()
            if delay < 0:
                next_at = loop.time()
                delay = 0.0
            await asyncio.sleep(delay)

    async def _handle_update(self, _client: Any, update: Any) -> None:
        api = self._api
        if api is None or getattr(update, "chat_id", None) != self._chat_id:
            return
        if isinstance(update, api.StreamFrames):
            if update.direction & api.Direction.INCOMING and update.device & api.Device.SPEAKER:
                for frame in update.frames:
                    self._on_recorded(frame.frame, len(frame.frame))
        elif isinstance(update, api.ChatUpdate):
            if update.status & api.ChatUpdate.Status.LEFT_CALL:
                log.warning("group call ended or dropped (status=%s)", update.status)
                if self._on_disconnect is not None:
                    self._on_disconnect()
