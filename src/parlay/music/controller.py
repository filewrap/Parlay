"""Per-runtime music command controller and playback snapshots."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .. import presentation as fmt
from ..audio.arbiter import AudioOutputArbiter
from ..media.resolver import ResolvedTrack, TrackResolver
from ..media.track import MediaError, TrackNotFoundError
from ..media.transcoder import MediaTranscoder
from ..session import CallSessionManager, ConnectionState
from .producer import MusicProducer
from .queue import TrackQueue

log = logging.getLogger(__name__)
MessagePoster = Callable[[str], Awaitable[None]]
Snapshot = dict[str, Any]
ChangeObserver = Callable[[Snapshot], Awaitable[None]]
_YOUTUBE_ID = re.compile(r"(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})")


class MusicController:
    """Drive one call's finite, non-repeating queue."""

    def __init__(
        self,
        sessions: CallSessionManager,
        arbiter: AudioOutputArbiter,
        resolver: TrackResolver,
        transcoder: MediaTranscoder,
        *,
        post_message: MessagePoster | None = None,
        on_change: ChangeObserver | None = None,
    ) -> None:
        self._sessions = sessions
        self._resolver = resolver
        self._post_message = post_message
        self._on_change = on_change
        self._queue = TrackQueue()
        self._producer = MusicProducer(arbiter, transcoder, on_finished=self._on_track_finished)
        self._lock = asyncio.Lock()
        self._observer_task: asyncio.Task[None] | None = None
        self._pending_snapshot: Snapshot | None = None
        self._observer_wakeup = asyncio.Event()

    @property
    def is_active(self) -> bool:
        return self._producer.current is not None

    def snapshot(self) -> Snapshot:
        current = self._producer.current
        status = "idle"
        if current is not None:
            status = "paused" if self._producer.is_paused else "playing"
        return {
            "track": self._track_snapshot(current) if current is not None else None,
            "status": status,
            "position_seconds": self._producer.position_seconds if current is not None else 0.0,
            "server_time": time.time(),
            "queue": [self._track_snapshot(item) for item in self._queue.pending],
        }

    async def play(self, request: str) -> str:
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
                reply = fmt.status(f"Now playing: {resolved.track.title}", "play")
            else:
                position = self._queue.enqueue(resolved)
                reply = fmt.status(f"Queued at #{position}: {resolved.track.title}", "queue")
            self._changed()
            return reply

    async def force_play(self, request: str) -> str:
        precondition = self._check_ready()
        if precondition is not None:
            return precondition
        try:
            resolved = await self._resolver.resolve(request)
        except TrackNotFoundError:
            return fmt.warning(f"No result found for {request!r}. Nothing was changed.")
        except MediaError as exc:
            return fmt.error(f"Could not resolve that track: {exc}")
        async with self._lock:
            await self._producer.stop()
            await self._start(resolved)
            self._changed()
        return fmt.status(f"Now playing: {resolved.track.title}", "play")

    async def skip(self) -> str:
        if not self.is_active:
            return fmt.warning("Nothing is playing.")
        async with self._lock:
            await self._producer.stop()
            nxt = self._queue.pop_next()
            if nxt is None:
                reply = fmt.status("Skipped. Queue is empty.", "stop")
            else:
                await self._start(nxt)
                reply = fmt.status(f"Now playing: {nxt.track.title}", "play")
            self._changed()
            return reply

    async def pause(self) -> str:
        if not self.is_active:
            return fmt.warning("Nothing is playing.")
        if self._producer.is_paused:
            return fmt.warning("Already paused.")
        await self._producer.pause()
        self._changed()
        return fmt.status("Paused.", "pause")

    async def resume(self) -> str:
        if not self.is_active:
            return fmt.warning("Nothing is playing.")
        if not self._producer.is_paused:
            return fmt.warning("Already playing.")
        await self._producer.resume()
        self._changed()
        return fmt.status("Resumed.", "play")

    async def stop(self) -> str:
        if not self.is_active and not self._queue.pending:
            return fmt.warning("Nothing is playing.")
        async with self._lock:
            await self._producer.stop()
            self._queue.clear()
            self._changed()
        return fmt.status("Stopped music and cleared the queue.", "stop")

    async def show_queue(self) -> str:
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
        async with self._lock:
            await self._producer.stop()
            self._queue.clear()
            self._changed()

    def _check_ready(self) -> str | None:
        session = self._sessions.session
        if session is None:
            return fmt.error("Join a voice chat first.")
        if session.state is not ConnectionState.CONNECTED:
            return fmt.error("The call is not ready yet.")
        return None

    async def _start(self, resolved: ResolvedTrack) -> None:
        await self._producer.play(resolved)
        await self._announce(f"{fmt.ICONS['play']} Now playing: {resolved.track.title}")

    async def _on_track_finished(
        self, producer: MusicProducer, track: ResolvedTrack, ok: bool
    ) -> None:
        async with self._lock:
            nxt = self._queue.pop_next()
            if nxt is None:
                await self._announce(f"{fmt.ICONS['stop']} Queue finished.")
            else:
                await self._start(nxt)
            self._changed()

    def _changed(self) -> None:
        """Coalesce observer backlog without awaiting it or holding playback locks."""
        if self._on_change is None:
            return
        self._pending_snapshot = self.snapshot()
        self._observer_wakeup.set()
        if self._observer_task is None or self._observer_task.done():
            self._observer_task = asyncio.create_task(self._deliver_changes())

    async def _deliver_changes(self) -> None:
        while self._pending_snapshot is not None:
            snapshot, self._pending_snapshot = self._pending_snapshot, None
            self._observer_wakeup.clear()
            try:
                assert self._on_change is not None
                await self._on_change(snapshot)
            except Exception:
                log.exception("playback observer failed")
            if self._pending_snapshot is None:
                await asyncio.sleep(0)

    @staticmethod
    def _track_snapshot(resolved: ResolvedTrack) -> Snapshot:
        track = resolved.track
        source_url = track.webpage_url or track.query
        match = _YOUTUBE_ID.search(source_url)
        item: Snapshot = {
            "id": match.group(1) if match else source_url,
            "title": track.title,
            "source_url": source_url,
        }
        if match:
            item["youtube_id"] = match.group(1)
        if track.duration_s is not None:
            item["duration"] = track.duration_s
        return item

    async def _announce(self, text: str) -> None:
        if self._post_message is None:
            return
        try:
            await self._post_message(text)
        except Exception:
            log.exception("failed to post in-call message")
