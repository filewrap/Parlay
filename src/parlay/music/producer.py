"""Paced PCM music producer with an observable playback cursor."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from ..audio.arbiter import AudioOutputArbiter
from ..audio.frames import CALL_CHANNELS, CALL_RATE, Pcm48kFrame
from ..media.resolver import ResolvedTrack
from ..media.track import TranscodeError
from ..media.transcoder import MediaTranscoder

log = logging.getLogger(__name__)

CompletionHandler = Callable[["MusicProducer", ResolvedTrack, bool], Awaitable[None]]
_BYTES_PER_SECOND = CALL_RATE * CALL_CHANNELS * 2


class MusicProducer:
    """Play one finite track through the per-call output arbiter."""

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
        self._gate = asyncio.Event()
        self._gate.set()
        self._paused = False
        self._sent_bytes = 0

    @property
    def current(self) -> ResolvedTrack | None:
        return self._current

    @property
    def is_playing(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def position_seconds(self) -> float:
        """PCM duration accepted by the paced producer for the current track."""
        return self._sent_bytes / _BYTES_PER_SECOND

    async def play(self, track: ResolvedTrack) -> None:
        await self.stop()
        await self._arbiter.acquire(self)
        self._current = track
        self._paused = False
        self._sent_bytes = 0
        self._gate.set()
        self._task = asyncio.create_task(self._run(track))

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self._transcoder.stop()
        self._current = None
        self._paused = False
        self._sent_bytes = 0
        self._gate.set()
        await self._arbiter.release(self)

    async def pause(self) -> None:
        self._paused = True
        self._gate.clear()

    async def resume(self) -> None:
        self._paused = False
        self._gate.set()

    async def _run(self, track: ResolvedTrack) -> None:
        completed = False
        try:
            async for chunk in self._transcoder.stream(track.stream.stream_url):
                await self._gate.wait()
                frame = Pcm48kFrame(pcm=chunk)
                self._sink.play_frame(frame)
                self._sent_bytes += len(chunk)
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
        self._paused = False
        await self._arbiter.release(self)
        if self._on_finished is not None:
            await self._on_finished(self, track, True)
