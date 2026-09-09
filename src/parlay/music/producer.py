"""MusicProducer: feeds a track's PCM through the Audio Output Arbiter.

The producer is one `AudioProducer` (the other being the AI Voice Pipeline).
It streams a resolved track through the `MediaTranscoder`, wraps each chunk in
a `Pcm48kFrame`, and pushes it to the call output through a `PlaybackHandle`
from the Arbiter, so music and AI never mix (REQ-INJ-006).

Playback is paced to real time: after each chunk the loop sleeps for the
chunk's duration. A pause gate lets the Arbiter (or the pause command) hold the
current position by stopping the feed; resume continues the same stream from
where it stopped (REQ-MUS-005.2/.3). When the stream ends on its own, the
producer reports completion so the controller can auto-advance (REQ-MUS-004.1).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from ..audio.arbiter import AudioOutputArbiter
from ..audio.frames import Pcm48kFrame
from ..media.resolver import ResolvedTrack
from ..media.track import TranscodeError
from ..media.transcoder import MediaTranscoder

log = logging.getLogger(__name__)

# Completion reason passed to the on_finished callback.
CompletionHandler = Callable[["MusicProducer", ResolvedTrack, bool], Awaitable[None]]


class MusicProducer:
    """Plays one track at a time into the call output via the Arbiter."""

    def __init__(
        self,
        arbiter: AudioOutputArbiter,
        transcoder: MediaTranscoder,
        *,
        on_finished: CompletionHandler | None = None,
    ) -> None:
        self._arbiter = arbiter
        self._transcoder = transcoder
        self._on_finished = on_finished
        self._sink = arbiter.handle_for(self)
        self._task: asyncio.Task[None] | None = None
        self._current: ResolvedTrack | None = None
        # Feed gate: set = play, cleared = paused. Starts open.
        self._gate = asyncio.Event()
        self._gate.set()
        self._paused = False

    @property
    def current(self) -> ResolvedTrack | None:
        return self._current

    @property
    def is_playing(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def is_paused(self) -> bool:
        return self._paused

    async def play(self, track: ResolvedTrack) -> None:
        """Acquire the call output and begin streaming `track`.

        Any track already playing on this producer is stopped first. The
        Arbiter handover (pausing the AI pipeline if it holds the output) is
        performed by `acquire` (REQ-MUS-002.3).
        """
        await self.stop()
        await self._arbiter.acquire(self)
        self._current = track
        self._paused = False
        self._gate.set()
        self._task = asyncio.create_task(self._run(track))

    async def stop(self) -> None:
        """Stop the current track and release the call output.

        Used by skip and stop; does not fire the completion callback.
        """
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self._transcoder.stop()
        self._current = None
        self._paused = False
        await self._arbiter.release(self)

    # --- AudioProducer contract -------------------------------------------
    async def pause(self) -> None:
        """Hold the current position by closing the feed gate (REQ-MUS-005.2)."""
        self._paused = True
        self._gate.clear()

    async def resume(self) -> None:
        """Continue feeding from the held position (REQ-MUS-005.3)."""
        self._paused = False
        self._gate.set()

    # --- internal feed loop ------------------------------------------------
    async def _run(self, track: ResolvedTrack) -> None:
        completed = False
        try:
            async for chunk in self._transcoder.stream(track.stream.stream_url):
                await self._gate.wait()  # blocks while paused, holding position
                frame = Pcm48kFrame(pcm=chunk)
                self._sink.play_frame(frame)
                # Pace to real time so the buffer is not flooded and pause holds.
                await asyncio.sleep(frame.duration_ms / 1000.0)
            completed = True
        except asyncio.CancelledError:
            raise
        except TranscodeError:
            log.exception("transcode failed for %s", track.track.title)
        except Exception:
            log.exception("music playback failed for %s", track.track.title)
        finally:
            if completed:
                await self._finish(track)

    async def _finish(self, track: ResolvedTrack) -> None:
        self._current = None
        self._task = None
        if self._on_finished is not None:
            await self._on_finished(self, track, True)
