"""TrackResolver: turn play text or a link into a resolved, streamable track.

The resolver is the entry point of the Media Sourcing Pipeline. For search text
it runs one public search to pick the best match, then one stream lookup; for a
YouTube link it resolves the stream directly by video id. Both paths lead with
the cookieless public front-ends and fall back to yt-dlp inside #SourceSelector,
so there is a single resolution round rather than a metadata probe followed by a
separate stream probe.

It reports not-found when a search matches nothing and surfaces a resolution
failure when every source fails, without altering any currently playing track
(that policy lives in the caller).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .source_selector import SourceSelector, _youtube_id
from .track import SourceResolutionError, StreamSource, Track, TrackNotFoundError

log = logging.getLogger(__name__)

_LINK_PREFIXES = ("http://", "https://")


@dataclass(frozen=True)
class ResolvedTrack:
    """A track paired with the media source that will stream it."""

    track: Track
    stream: StreamSource


class TrackResolver:
    """Builds a Track from a request and resolves it to a StreamSource."""

    def __init__(self, selector: SourceSelector) -> None:
        self._selector = selector

    async def resolve(self, request: str) -> ResolvedTrack:
        """Resolve search text or a link into a streamable track."""
        query = request.strip()
        if not query:
            raise TrackNotFoundError("empty play request")
        if query.startswith(_LINK_PREFIXES):
            return await self._resolve_link(query)
        return await self._resolve_search(query)

    async def _resolve_search(self, query: str) -> ResolvedTrack:
        items = await self._selector.search(query, 1)
        if not items:
            raise TrackNotFoundError(f"no track matched {query!r}")
        item = items[0]
        video_id = str(item["youtube_id"])
        resolved = await self._selector.resolve_by_id(video_id)
        if resolved is None:
            raise SourceResolutionError(
                f"no media source could stream {query!r}. Every public instance "
                "and the yt-dlp fallback failed; check the media provider logs."
            )
        track = Track(
            title=str(item.get("title") or query),
            query=query,
            webpage_url=item["source_url"],
            duration_s=item.get("duration") or resolved.duration_s,
        )
        return ResolvedTrack(track=track, stream=resolved.stream)

    async def _resolve_link(self, query: str) -> ResolvedTrack:
        video_id = _youtube_id(query)
        if video_id is not None:
            resolved = await self._selector.resolve_by_id(video_id)
            if resolved is None:
                raise SourceResolutionError(
                    f"no media source could stream {query!r}. Every public instance "
                    "and the yt-dlp fallback failed; check the media provider logs."
                )
            track = Track(
                title=str(resolved.title or query),
                query=query,
                webpage_url=f"https://www.youtube.com/watch?v={video_id}",
                duration_s=resolved.duration_s,
            )
            return ResolvedTrack(track=track, stream=resolved.stream)
        # Non-YouTube direct link: resolve the raw URL through the yt-dlp fallback.
        track = Track(title=query, query=query, webpage_url=query)
        stream = await self._selector.resolve(track)
        return ResolvedTrack(track=track, stream=stream)
