"""Playback: outbound callback handling backed by a bounded buffer.

The outbound ntgcalls callback runs on the native audio thread and must return
exactly the requested byte count every cycle. It reads only from the
PlaybackBuffer (no blocking work), substituting silence when empty.

Producers (AI pipeline, file playback) enqueue provider audio, which is
up-converted to 48 kHz stereo before entering the buffer.
"""

from __future__ import annotations

import logging

from .buffers import OverflowPolicy, PlaybackBuffer
from .frames import AudioChunk, Pcm48kFrame
from .resampler import AudioResampler

log = logging.getLogger(__name__)


class PlaybackService:
    """Owns the outbound raw callback and the playback buffer."""

    def __init__(
        self,
        resampler: AudioResampler | None = None,
        max_ms: int = 2000,
        policy: str = OverflowPolicy.DROP_OLDEST,
    ) -> None:
        self._resampler = resampler or AudioResampler()
        self._buffer = PlaybackBuffer(max_ms=max_ms, policy=policy)

    # --- native-thread side ---------------------------------------------
    def on_played_data(self, length: int) -> bytes:
        """ntgcalls outbound callback. Runs on the native audio thread.

        Returns exactly `length` bytes, padding with silence when the buffer
        is short (AC-INJ-001.1/.2). No blocking work (AC-INJ-001.3).
        """
        return self._buffer.take(length)

    # --- asyncio / producer side ----------------------------------------
    def enqueue_chunk(self, chunk: AudioChunk) -> None:
        """Enqueue provider audio, up-converting to the 48 kHz call format."""
        frame = self._resampler.to_call(chunk)
        if frame.pcm:
            self._buffer.enqueue(frame)

    def enqueue_frame(self, frame: Pcm48kFrame) -> None:
        """Enqueue audio already at the 48 kHz call format."""
        if frame.pcm:
            self._buffer.enqueue(frame)

    def flush(self) -> None:
        """Clear pending audio at once (used on interruption)."""
        self._buffer.flush()

    def clear(self) -> None:
        """Release buffered audio when the session ends (AC-INJ-005.3)."""
        self._buffer.flush()

    @property
    def queued_bytes(self) -> int:
        return len(self._buffer)
