"""Bounded server-side media search for room playback, companion, and /play.

Search leads with the cookieless public front-ends (Invidious, then Piped) via
#PublicSourceClient and falls back to a cookieless yt-dlp search only when every
public instance fails. Direct links are validated to public HTTPS YouTube hosts
and resolved by video id. Results are cached briefly and concurrency is bounded.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .media.po_token import PoTokenProvider
from .media.public_sources import PublicSourceClient, _item
from .media.source_selector import SourceSelector
from .rooms.service import RoomError

_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_HOSTS = {"youtube.com", "www.youtube.com", "music.youtube.com", "m.youtube.com", "youtu.be"}


class MediaSearch:
    def __init__(
        self,
        pot_provider_url: str = "http://127.0.0.1:4416",
        *,
        public: PublicSourceClient | None = None,
        selector: SourceSelector | None = None,
    ) -> None:
        self._public = public or PublicSourceClient()
        self._selector = selector or SourceSelector(
            PoTokenProvider(pot_provider_url), public=self._public
        )
        self._slots = asyncio.Semaphore(2)
        self._cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}

    async def __call__(self, query: str) -> list[dict[str, Any]]:
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise RoomError("invalid_query", "Enter a song name or YouTube link")
        query = query.strip()
        video_id = self._link_video_id(query)
        cache_key = f"id:{video_id}" if video_id else f"q:{query}"
        async with self._slots:
            cached = self._cache.get(cache_key)
            if cached and cached[0] > time.monotonic():
                return [dict(item) for item in cached[1]]
            try:
                if video_id is not None:
                    results = await self._resolve_link(video_id)
                else:
                    results = await self._selector.search(query, 10)
            except RoomError:
                raise
            except Exception as exc:
                raise RoomError(
                    "search_failed", "Music search is temporarily unavailable", 503
                ) from exc
            if len(self._cache) >= 256:
                self._cache.pop(next(iter(self._cache)))
            self._cache[cache_key] = (time.monotonic() + 120, results)
            return [dict(item) for item in results]

    def _link_video_id(self, query: str) -> str | None:
        url = urlsplit(query)
        if not url.scheme and not query.startswith("//"):
            return None
        if (
            url.scheme != "https"
            or url.hostname not in _HOSTS
            or url.username
            or url.password
            or url.port not in (None, 443)
        ):
            raise RoomError("unsupported_source", "Use a song name or HTTPS YouTube video link")
        video_id = (
            url.path.strip("/")
            if url.hostname == "youtu.be"
            else parse_qs(url.query).get("v", [""])[0]
        )
        if not _ID.fullmatch(video_id):
            raise RoomError("unsupported_source", "Use a direct YouTube video link")
        return video_id

    async def _resolve_link(self, video_id: str) -> list[dict[str, Any]]:
        resolved = await self._public.resolve(video_id)
        title = resolved.title if resolved is not None else None
        duration = resolved.duration_s if resolved is not None else None
        return [_item(video_id, title, "", duration)]
