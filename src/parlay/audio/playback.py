"""Playback: outbound callback handling backed by a bounded buffer.

The outbound ntgcalls callback runs on the native audio thread and must return
exactly the requested byte count every cycle. It reads only from the
PlaybackBuffer (no blocking work), substituting silence when empty.

Producers (AI pipeline, file playback) enqueue provider audio, which is
up-converted to 48 kHz stereo before entering the buffer. The buffer runs as a
jitter buffer (see PlaybackBuffer): it holds a short cushion before draining a
run of audio and re-fills the cushion on an underrun, so bursty provider audio
plays smoothly instead of shattering. `mark_ready` releases a run shorter than
the cushion at its turn boundary.
"""

from __future__ import annotations

import logging

from .buffers import OverflowPolicy, PlaybackBuffer
from .frames import AudioChunk, Pcm48kFrame
from .resampler import AudioResampler

log = logging.getLogger(__name__)

# Cushion held before a run of audio begins draining. Enough to absorb the
# burstiness of native-audio replies without adding much latency.
DEFAULT_PREBUFFER_MS = 200
# High safety ceiling: audio is only discarded past this, so ordinary jitter
# never drops. Far above the cushion so it does not interfere with pacing.
DEFAULT_MAX_MS = 6000


class PlaybackService:
    """Owns the outbound raw callback and the playback buffer."""

    def __init__(
        self,
        resampler: AudioResampler | None = None,
        max_ms: int = DEFAULT_MAX_MS,
        policy: str = OverflowPolicy.DROP_OLDEST,
        prebuffer_ms: int = DEFAULT_PREBUFFER_MS,
    ) -> None:
        self._resampler = resampler or AudioResampler()
        self._buffer = PlaybackBuffer(max_ms=max_ms, policy=policy, prebuffer_ms=prebuffer_ms)

    # --- native-thread side ---------------------------------------------
    def on_played_data(self, length: int) -> bytes:
        """ntgcalls outbound callback. Runs on the native audio thread.

        Returns exactly `length` bytes, padding with silence when the buffer
        is short or still priming (AC-INJ-001.1/.2). No blocking work
        (AC-INJ-001.3).
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

    def mark_ready(self) -> None:
        """Release buffered audio now even if the cushion is not yet full."""
        self._buffer.mark_ready()

    def flush(self) -> None:
        """Clear pending audio at once (used on interruption)."""
        self._buffer.flush()

    def clear(self) -> None:
        """Release buffered audio when the session ends (AC-INJ-005.3)."""
        self._buffer.flush()

    @property
    def queued_bytes(self) -> int:
        return len(self._buffer)
