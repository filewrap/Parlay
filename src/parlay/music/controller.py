"""MusicController: handles music commands, owns the queue, and messages the call.

The controller is the music feature's command layer. It enforces playback
preconditions (active, connected Call Session, and Arbiter handover before
playing), resolves each Play Command through the Media Sourcing Pipeline,
starts playback or enqueues, drives auto-advance on track completion, and posts
now-playing and error messages to the call chat (REQ-MUS-001, 002, 004, 005,
006).

It owns one #MusicProducer (the AudioProducer the Arbiter arbitrates) and one
#TrackQueue per Call Session. Source resolution and transcoding come from
injected collaborators, so this module stays free of yt-dlp/ffmpeg.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from .. import presentation as fmt
from ..audio.arbiter import AudioOutputArbiter
from ..media.resolver import ResolvedTrack, TrackResolver
from ..media.track import MediaError, TrackNotFoundError
from ..media.transcoder import MediaTranscoder
from ..session import CallSessionManager, ConnectionState
from .producer import MusicProducer
from .queue import TrackQueue

log = logging.getLogger(__name__)

# Posts an in-call message to the current Call Session chat.
MessagePoster = Callable[[str], Awaitable[None]]


class MusicController:
    """Drives play/skip/pause/resume/stop/queue and the per-session queue."""

    def __init__(
        self,
        sessions: CallSessionManager,
        arbiter: AudioOutputArbiter,
        resolver: TrackResolver,
        transcoder: MediaTranscoder,
        *,
        post_message: MessagePoster | None = None,
    ) -> None:
        self._sessions = sessions
        self._resolver = resolver
        self._post_message = post_message
        self._queue = TrackQueue()
        self._producer = MusicProducer(arbiter, transcoder, on_finished=self._on_track_finished)
        self._lock = asyncio.Lock()

    @property
    def is_active(self) -> bool:
        """True while a track is playing or paused."""
        return self._producer.current is not None

    async def play(self, request: str) -> str:
        """Resolve a Play Command and start playback or enqueue (REQ-MUS-001)."""
        precondition = self._check_ready()
        if precondition is not None:
            return precondition
        if not request.strip():
            return fmt.error("Give a song name or link to play.")
        try:
            resolved = await self._resolver.resolve(request)
        except TrackNotFoundError:
            return fmt.warning(f"No result found for {request!r}. Nothing was changed.")
        except MediaError as exc:
            return fmt.error(f"Could not resolve that track: {exc}")
        async with self._lock:
            if self._producer.current is None:
                await self._start(resolved)
                return fmt.status(f"Now playing: {resolved.track.title}", "play")
            position = self._queue.enqueue(resolved)
        return fmt.status(f"Queued at #{position}: {resolved.track.title}", "queue")

    async def skip(self) -> str:
        """Stop the current track and advance to the next, or silence (REQ-MUS-005.1)."""
        if not self.is_active:
            return fmt.warning("Nothing is playing.")
        async with self._lock:
            await self._producer.stop()
            nxt = self._queue.pop_next()
            if nxt is None:
                return fmt.status("Skipped. Queue is empty.", "stop")
            await self._start(nxt)
            return fmt.status(f"Now playing: {nxt.track.title}", "play")

    async def pause(self) -> str:
        """Pause playback, holding position (REQ-MUS-005.2)."""
        if not self.is_active:
            return fmt.warning("Nothing is playing.")
        if self._producer.is_paused:
            return fmt.warning("Already paused.")
        await self._producer.pause()
        return fmt.status("Paused.", "pause")

    async def resume(self) -> str:
        """Resume from the held position (REQ-MUS-005.3)."""
        if not self.is_active:
            return fmt.warning("Nothing is playing.")
        if not self._producer.is_paused:
            return fmt.warning("Already playing.")
        await self._producer.resume()
        return fmt.status("Resumed.", "play")

    async def stop(self) -> str:
        """Stop the current track, clear the queue, return to silence (REQ-MUS-005.4)."""
        if not self.is_active:
            return fmt.warning("Nothing is playing.")
        async with self._lock:
            await self._producer.stop()
            self._queue.clear()
        return fmt.status("Stopped music and cleared the queue.", "stop")

    async def show_queue(self) -> str:
        """Report the playing track and the ordered pending tracks (REQ-MUS-004.3)."""
        current = self._producer.current
        if current is None:
            return fmt.warning("Nothing is playing.")
        lines = [f"Now playing: {current.track.title}"]
        pending = self._queue.pending
        if pending:
            lines.append("Up next:")
            lines.extend(f"  {i}. {r.track.title}" for i, r in enumerate(pending, start=1))
        else:
            lines.append("Queue is empty.")
        return fmt.status("\n".join(lines), "queue")

    async def on_session_end(self) -> None:
        """Stop playback and clear the queue when the Call Session ends (REQ-MUS-004.4)."""
        await self._producer.stop()
        self._queue.clear()

    # --- internal ----------------------------------------------------------
    def _check_ready(self) -> str | None:
        """Return an error reply if playback preconditions fail (REQ-MUS-002)."""
        session = self._sessions.session
        if session is None:
            return fmt.error("Join a voice chat first.")
        if session.state is not ConnectionState.CONNECTED:
            return fmt.error("The call is not ready yet.")
        return None

    async def _start(self, resolved: ResolvedTrack) -> None:
        """Begin playback of a resolved track and announce it (REQ-MUS-006.1)."""
        await self._producer.play(resolved)
        await self._announce(f"{fmt.ICONS['play']} Now playing: {resolved.track.title}")

    async def _on_track_finished(
        self, producer: MusicProducer, track: ResolvedTrack, ok: bool
    ) -> None:
        """Auto-advance to the next track, or return to silence (REQ-MUS-004.1/.2)."""
        async with self._lock:
            nxt = self._queue.pop_next()
            if nxt is None:
                await self._announce(f"{fmt.ICONS['stop']} Queue finished.")
                return
            await self._start(nxt)

    async def _announce(self, text: str) -> None:
        if self._post_message is None:
            return
        try:
            await self._post_message(text)
        except Exception:
            log.exception("failed to post in-call message")
