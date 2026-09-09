"""Raw Audio Bridge: composes capture, playback, and the raw-call adapter.

This is the single object the session lifecycle drives. `start(chat)` brings up
the raw group call wired to the capture and playback services; `stop()` tears
it down and releases all buffers. Consumers subscribe to the Captured Stream
and producers enqueue into the Playback Sink.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .capture import AudioCaptureService, ConsumerCallback
from .frames import AudioChunk, Pcm48kFrame
from .playback import PlaybackService
from .rawcall import RawCallAdapter
from .resampler import AudioResampler

log = logging.getLogger(__name__)


class RawAudioBridge:
    """Owns one direction-pair of the raw path for a Call Session."""

    def __init__(self, client: Any) -> None:
        resampler = AudioResampler()
        self._capture = AudioCaptureService(resampler=resampler)
        self._playback = PlaybackService(resampler=resampler)
        self._adapter = RawCallAdapter(
            client,
            on_recorded=self._capture.on_recorded_data,
            on_played=self._playback.on_played_data,
        )
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    # --- Captured Stream (inbound) --------------------------------------
    def subscribe(self, callback: ConsumerCallback, rate: int, channels: int) -> int:
        return self._capture.registry.subscribe(callback, rate, channels)

    def unsubscribe(self, token: int) -> None:
        self._capture.registry.unsubscribe(token)

    # --- Playback Sink (outbound) ---------------------------------------
    def play_chunk(self, chunk: AudioChunk) -> None:
        self._playback.enqueue_chunk(chunk)

    def play_frame(self, frame: Pcm48kFrame) -> None:
        self._playback.enqueue_frame(frame)

    def interrupt(self) -> None:
        """Clear pending playback at once (used on AI interruption)."""
        self._playback.flush()

    # --- lifecycle ------------------------------------------------------
    async def start(self, chat: Any) -> None:
        if self._active:
            raise RuntimeError("bridge already active")
        self._capture.start(asyncio.get_event_loop())
        await self._adapter.start(chat)
        self._active = True
        log.info("raw audio bridge active")

    async def stop(self) -> None:
        if not self._active:
            return
        try:
            await self._adapter.stop()
        finally:
            await self._capture.stop()
            self._playback.clear()
            self._active = False
            log.info("raw audio bridge stopped; buffers released")
