"""TrackQueue: the per-Call-Session ordered list of pending tracks.

The queue holds `ResolvedTrack` items waiting to play. The currently playing
track is owned by the MusicController, not stored here, so the queue models
only what is pending. It auto-advances by popping the head and is cleared when
the Call Session ends (REQ-MUS-004).
"""

from __future__ import annotations

from collections import deque

from ..media.resolver import ResolvedTrack


class TrackQueue:
    """An ordered queue of resolved tracks pending playback."""

    def __init__(self) -> None:
        self._items: deque[ResolvedTrack] = deque()

    def __len__(self) -> int:
        return len(self._items)

    def enqueue(self, track: ResolvedTrack) -> int:
        """Append a track and return its 1-based position among pending tracks."""
        self._items.append(track)
        return len(self._items)

    def pop_next(self) -> ResolvedTrack | None:
        """Remove and return the head track, or None when the queue is empty."""
        if not self._items:
            return None
        return self._items.popleft()

    def clear(self) -> None:
        """Drop all pending tracks (Call Session end or stop)."""
        self._items.clear()

    @property
    def pending(self) -> list[ResolvedTrack]:
        """The ordered pending tracks, not including the playing track."""
        return list(self._items)
