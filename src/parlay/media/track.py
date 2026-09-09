"""Core media types: a resolved Track, a streamable source, and errors.

A `Track` is what the resolver builds from a play request: a title, the source
reference it came from, and (when known) a duration. A `StreamSource` is the
outcome of Source Resolution: the concrete media source that served the track
and a direct stream URL the transcoder can read. Errors distinguish "nothing
matched the query" from "every media source failed".
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class MediaSource(StrEnum):
    """Media sources tried during Source Resolution, in priority order."""

    YOUTUBE = "youtube"
    INVIDIOUS = "invidious"
    PIPED = "piped"


# Priority order for Source Resolution (AC-MUS-003.1).
SOURCE_PRIORITY: tuple[MediaSource, ...] = (
    MediaSource.YOUTUBE,
    MediaSource.INVIDIOUS,
    MediaSource.PIPED,
)


class MediaError(RuntimeError):
    """Base class for media sourcing failures."""


class TrackNotFoundError(MediaError):
    """Raised when a search or link resolves to no playable track."""


class SourceResolutionError(MediaError):
    """Raised when every Media Source failed for a track (AC-MUS-003.3)."""


class TranscodeError(MediaError):
    """Raised when ffmpeg cannot transcode the resolved stream."""


@dataclass(frozen=True)
class Track:
    """A resolvable track built from search text or a link."""

    title: str
    query: str  # the original search text or link
    webpage_url: str | None = None
    duration_s: float | None = None


@dataclass(frozen=True)
class StreamSource:
    """A resolved, streamable media source for a track."""

    source: MediaSource
    stream_url: str
    abr: float | None = None  # average audio bitrate the source offered, in kbps
