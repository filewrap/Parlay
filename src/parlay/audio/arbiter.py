"""Audio Output Arbiter: grants the single call output to one producer at a time.

The two producers, the AI Voice Pipeline (reply audio) and Music Playback
(tracks), both want the one outbound path. The Arbiter grants the Playback Sink
to exactly one `AudioProducer`, pauses the holder on handover, remembers it, and
resumes it when the new holder releases. It flushes the buffer on every handover
so no two producers' audio mixes, and serves silence (the buffer drains to
silence on its own) when no producer holds the output. This enforces the strict
mutual exclusion between the AI pipeline and music (REQ-INJ-006).

Producers no longer enqueue to the Playback Sink directly. They enqueue through
the Arbiter, which drops any audio from a producer that does not currently hold
the output. Each producer hands the Arbiter a `PlaybackHandle` (satisfying the
`PlaybackSink` protocol) so existing producers keep their sink-shaped calls.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from .frames import AudioChunk, Pcm48kFrame

log = logging.getLogger(__name__)


class AudioProducer(Protocol):
    """A source that can drive the call output and be paused/resumed by the Arbiter.

    The pause/resume contract is delegated to each producer: the Arbiter calls
    these hooks on handover and does not itself know how to pause a producer.
    """

    async def pause(self) -> None: ...

    async def resume(self) -> None: ...


class PlaybackTarget(Protocol):
    """The single call output the Arbiter owns (satisfied by RawAudioBridge)."""

    def play_chunk(self, chunk: AudioChunk) -> None: ...

    def play_frame(self, frame: Pcm48kFrame) -> None: ...

    def interrupt(self) -> None: ...


class AudioOutputArbiter:
    """Single gate over the call output enforcing one-producer-at-a-time."""

    def __init__(self, target: PlaybackTarget) -> None:
        self._target = target
        self._holder: AudioProducer | None = None
        self._paused: AudioProducer | None = None
        self._lock = asyncio.Lock()

    @property
    def holder(self) -> AudioProducer | None:
        return self._holder

    async def acquire(self, producer: AudioProducer) -> None:
        """Grant the call output to `producer`, preempting any current holder.

        If another producer holds the output, pause it and remember it so it can
        be resumed when `producer` releases (AC-INJ-006.1/.2). The buffer is
        flushed on handover so the previous producer's audio is not mixed
        (AC-INJ-006.4).
        """
        async with self._lock:
            if self._holder is producer:
                return
            if self._holder is not None:
                await self._holder.pause()
                self._paused = self._holder
            self._target.interrupt()
            self._holder = producer
            log.info("call output granted to %s", type(producer).__name__)

    async def release(self, producer: AudioProducer) -> None:
        """Release the call output held by `producer`.

        Flush the buffer on handover, then, if a paused producer was remembered,
        resume it from where it paused (AC-INJ-006.3/.4). When nothing is
        remembered the output goes idle and the buffer drains to silence
        (AC-INJ-006.5).
        """
        async with self._lock:
            if self._holder is not producer:
                return
            self._target.interrupt()
            self._holder = None
            if self._paused is not None:
                resumed = self._paused
                self._paused = None
                self._holder = resumed
                await resumed.resume()
                log.info("call output resumed by %s", type(resumed).__name__)
            else:
                log.info("call output released; now idle")

    # --- producer-facing sink -------------------------------------------
    def play_chunk(self, producer: AudioProducer, chunk: AudioChunk) -> None:
        """Enqueue a chunk only if `producer` currently holds the output."""
        if self._holder is producer:
            self._target.play_chunk(chunk)

    def play_frame(self, producer: AudioProducer, frame: Pcm48kFrame) -> None:
        """Enqueue a frame only if `producer` currently holds the output."""
        if self._holder is producer:
            self._target.play_frame(frame)

    def flush(self, producer: AudioProducer) -> None:
        """Flush pending playback only if `producer` currently holds the output."""
        if self._holder is producer:
            self._target.interrupt()

    def handle_for(self, producer: AudioProducer) -> PlaybackHandle:
        """Return a sink-shaped handle a producer passes to its own machinery."""
        return PlaybackHandle(self, producer)


class PlaybackHandle:
    """A per-producer view of the call output satisfying the PlaybackSink shape.

    Producers built against `play_chunk(chunk)` / `interrupt()` (such as the AI
    pipeline's ProviderSessionManager) hold one of these instead of the raw
    bridge, so their audio is gated by the Arbiter without code changes.
    """

    def __init__(self, arbiter: AudioOutputArbiter, producer: AudioProducer) -> None:
        self._arbiter = arbiter
        self._producer = producer

    def play_chunk(self, chunk: AudioChunk) -> None:
        self._arbiter.play_chunk(self._producer, chunk)

    def play_frame(self, frame: Pcm48kFrame) -> None:
        self._arbiter.play_frame(self._producer, frame)

    def interrupt(self) -> None:
        self._arbiter.flush(self._producer)
