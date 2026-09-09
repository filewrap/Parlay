"""TrackResolver: turn play text or a link into a resolved, streamable track.

The resolver is the entry point of the Media Sourcing Pipeline. It builds a
`Track` from a search query or a link (running a `ytsearch:` search for plain
text), then drives the `SourceSelector` to obtain a streamable URL across the
fallback sources. It reports not-found when a search matches nothing and
surfaces a resolution failure when every source fails, without altering any
currently playing track (that policy lives in the caller).

The metadata lookup uses yt-dlp lazily in a thread, mirroring SourceSelector,
so importing this module pulls no native/network surface.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from .source_selector import SourceSelector
from .track import StreamSource, Track, TrackNotFoundError

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
        track = await self._build_track(query)
        stream = await self._selector.resolve(track)
        return ResolvedTrack(track=track, stream=stream)

    async def _build_track(self, query: str) -> Track:
        is_link = query.startswith(_LINK_PREFIXES)
        target = query if is_link else f"ytsearch1:{query}"
        info = await asyncio.to_thread(self._probe, target)
        if info is None:
            raise TrackNotFoundError(f"no track matched {query!r}")
        return Track(
            title=str(info.get("title") or query),
            query=query,
            webpage_url=info.get("webpage_url") or info.get("url"),
            duration_s=info.get("duration"),
        )

    @staticmethod
    def _probe(target: str) -> dict[str, Any] | None:
        from yt_dlp import YoutubeDL  # lazy: avoids import at module load

        opts = {"quiet": True, "no_warnings": True, "skip_download": True}
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(target, download=False)
        if info is None:
            return None
        if "entries" in info:
            entries = [e for e in info["entries"] if e]
            if not entries:
                return None
            return entries[0]
        return info
