"""Captured Stream: inbound callback handling and consumer fan-out.

The inbound ntgcalls callback runs on the native audio thread and only pushes
frames into a bounded queue (no blocking work). A drain task on the asyncio
loop resamples each frame per consumer and delivers it in arrival order.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from .buffers import CaptureQueue
from .frames import CALL_FRAME_BYTES, AudioChunk, Pcm48kFrame
from .resampler import AudioResampler

log = logging.getLogger(__name__)

# A consumer receives resampled AudioChunks. Delivery is awaited but must be
# fast; slow consumers lose audio to the bounded queue upstream, never block
# the call.
ConsumerCallback = Callable[[AudioChunk], Awaitable[None]]


@dataclass
class _Consumer:
    callback: ConsumerCallback
    rate: int
    channels: int


class CapturedStreamRegistry:
    """Subscribe/unsubscribe consumers of the Captured Stream."""

    def __init__(self) -> None:
        self._consumers: dict[int, _Consumer] = {}
        self._next_id = 0

    def subscribe(self, callback: ConsumerCallback, rate: int, channels: int) -> int:
        token = self._next_id
        self._next_id += 1
        self._consumers[token] = _Consumer(callback, rate, channels)
        return token

    def unsubscribe(self, token: int) -> None:
        self._consumers.pop(token, None)

    def __len__(self) -> int:
        return len(self._consumers)

    def snapshot(self) -> list[_Consumer]:
        return list(self._consumers.values())


class AudioCaptureService:
    """Owns the inbound raw callback and fans audio out to consumers."""

    def __init__(
        self,
        resampler: AudioResampler | None = None,
        max_ms: int = 400,
    ) -> None:
        self._resampler = resampler or AudioResampler()
        self._queue = CaptureQueue(max_ms=max_ms)
        self._registry = CapturedStreamRegistry()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._drain_task: asyncio.Task[None] | None = None
        self._running = False

    @property
    def registry(self) -> CapturedStreamRegistry:
        return self._registry

    # --- native-thread side ---------------------------------------------
    def on_recorded_data(self, frame: bytes, length: int) -> None:
        """ntgcalls inbound callback. Runs on the native audio thread.

        Must not block. Tolerates unexpected frame sizes (AC-CAP-003.3): it
        stores whatever arrived and lets the resampler handle the length.
        """
        if not self._running:
            return
        data = frame[:length] if length and length <= len(frame) else frame
        if not data:
            return
        if len(data) != CALL_FRAME_BYTES:
            log.debug("unexpected inbound frame size: %d bytes", len(data))
        self._queue.push(Pcm48kFrame(pcm=bytes(data)))

    # --- asyncio side ---------------------------------------------------
    def start(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._loop = loop or asyncio.get_event_loop()
        self._running = True
        self._drain_task = self._loop.create_task(self._drain())

    async def _drain(self) -> None:
        try:
            while self._running:
                frame = self._queue.pop()
                if frame is None:
                    await asyncio.sleep(0.005)
                    continue
                await self._deliver(frame)
        except asyncio.CancelledError:  # graceful shutdown
            pass

    async def _deliver(self, frame: Pcm48kFrame) -> None:
        consumers = self._registry.snapshot()
        if not consumers:
            return  # discard when no consumer (AC-CAP-002.2)
        for consumer in consumers:
            chunk = self._resampler.capture_to(frame, consumer.rate, consumer.channels)
            try:
                await consumer.callback(chunk)
            except Exception:  # a broken consumer must not kill capture
                log.exception("consumer callback failed; continuing")

    async def stop(self) -> None:
        """Stop draining and release buffers (AC-CAP-005.2)."""
        self._running = False
        if self._drain_task is not None:
            self._drain_task.cancel()
            try:
                await self._drain_task
            except asyncio.CancelledError:
                pass
            self._drain_task = None
        self._queue.clear()
