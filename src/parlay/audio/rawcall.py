"""Adapter over py-tgcalls (pytgcalls org, NTgCalls-based) group calls.

This is the ONLY module that touches the pytgcalls API surface. Everything
else in `parlay.audio` is pure Python and unit-tested. The binding is
imported lazily inside `_load_api()` so the pure-Python core and its tests
never require the native wheel to be importable.

Verified against py-tgcalls 2.3.x / ntgcalls 2.2.x source:
    app = PyTgCalls(telethon_client)
    await app.start()
    await app.play(chat_id, MediaStream(ExternalMedia.AUDIO, ...))
        # joins the call with an EXTERNAL audio source; we push frames
        # ourselves with send_frame().
    await app.record(chat_id, RecordStream(audio=True, ...))
        # incoming audio arrives as StreamFrames updates on the asyncio loop.
    await app.send_frame(chat_id, Device.MICROPHONE, pcm_bytes)
    app.add_handler(handler)
    app.remove_handler(handler)
    await app.leave_call(chat_id)

Unexpected drops surface as a ChatUpdate whose status matches the composite
flag ChatUpdate.Status.LEFT_CALL (kicked / left / closed / discarded / busy).

Audio boundary: S16LE, 48 kHz, stereo, 10 ms frames (1920 bytes per frame).
AudioQuality.HIGH is (48000, 2), which is exactly this boundary.
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
# Fired when the call ends unexpectedly. Runs on the asyncio loop (pytgcalls
# dispatches updates there); the bridge still marshals defensively.
DisconnectHandler = Callable[[], None]

# One PyTgCalls engine per Telethon client. Creating a second engine for the
# same client would re-bind MTProto handlers, so the engine is cached and
# started exactly once per client.
_apps: dict[int, Any] = {}
_started: set[int] = set()


def _load_api() -> SimpleNamespace:
    """Import the py-tgcalls surface lazily and hand back the symbols we use."""
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
    """Return the cached PyTgCalls engine for this client, creating it once."""
    key = id(client)
    app = _apps.get(key)
    if app is None:
        app = api.PyTgCalls(client)
        _apps[key] = app
    return app


class RawCallAdapter:
    """Joins a group call and moves 10 ms PCM frames in both directions.

    Outbound is push-model: a pacing task pulls a frame from `on_played` every
    10 ms and pushes it with `send_frame`. Inbound `StreamFrames` updates are
    forwarded to `on_recorded` one frame at a time. A `ChatUpdate` matching
    `LEFT_CALL` fires `on_disconnect`.
    """

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
        app = _get_app(api, self._client)
        key = id(self._client)
        if key not in _started:
            await app.start()
            _started.add(key)
        self._chat_id = await app.resolve_chat_id(chat)
        app.add_handler(self._handle_update)
        self._app = app  # Keep cleanup possible if play/record fails or is cancelled.
        await app.play(
            self._chat_id,
            api.MediaStream(api.ExternalMedia.AUDIO, audio_parameters=api.AudioQuality.HIGH),
        )
        await app.record(
            self._chat_id,
            api.RecordStream(audio=True, audio_parameters=api.AudioQuality.HIGH),
        )
        self._app = app
        self._pump_task = asyncio.get_running_loop().create_task(self._pump())
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
        try:
            await app.leave_call(self._chat_id)
        except Exception:
            log.warning("leave_call failed; probably already out of the call", exc_info=True)
        finally:
            self._chat_id = None
            log.info("left group call")

    async def _pump(self) -> None:
        """Push one 10 ms frame per tick, pacing against the loop clock."""
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
                # Fell behind (event-loop stall); resync instead of bursting.
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
