"""Bounded server-side YouTube metadata search for room playback and Compass."""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .media.po_token import PoTokenProvider
from .media.source_selector import SourceSelector
from .rooms.service import RoomError

_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_HOSTS = {"youtube.com", "www.youtube.com", "music.youtube.com", "m.youtube.com", "youtu.be"}


class MediaSearch:
    def __init__(self, pot_provider_url: str = "http://127.0.0.1:4416") -> None:
        self._selector = SourceSelector(PoTokenProvider(pot_provider_url))
        self._slots = asyncio.Semaphore(2)
        self._cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}

    async def __call__(self, query: str) -> list[dict[str, Any]]:
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise RoomError("invalid_query", "Enter a song name or YouTube link")
        query = query.strip()
        url = urlsplit(query)
        if url.scheme or query.startswith("//"):
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
            target = f"https://www.youtube.com/watch?v={video_id}"
        else:
            # Always prefix text ourselves. Do not accept extractor pseudo-URLs.
            target = f"ytsearch10:{query}"
        async with self._slots:
            cached = self._cache.get(target)
            if cached and cached[0] > time.monotonic():
                return [dict(item) for item in cached[1]]
            try:
                tracks = await asyncio.to_thread(self._probe, target)
            except Exception as exc:
                raise RoomError(
                    "search_failed", "Music search is temporarily unavailable", 503
                ) from exc
            if len(self._cache) >= 256:
                self._cache.pop(next(iter(self._cache)))
            self._cache[target] = (time.monotonic() + 120, tracks)
            return [dict(item) for item in tracks]

    def _probe(self, target: str) -> list[dict[str, Any]]:
        from yt_dlp import YoutubeDL

        options = {
            **self._selector.youtube_options(),
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": "in_playlist",
            "noplaylist": True,
            "socket_timeout": 10,
            "retries": 1,
            "extractor_retries": 1,
        }
        with YoutubeDL(options) as client:
            result = client.extract_info(target, download=False)
        if not result:
            return []
        entries = result.get("entries") if "entries" in result else [result]
        output = []
        for entry in entries or []:
            if not entry:
                continue
            video_id = str(entry.get("id", ""))
            if not _ID.fullmatch(video_id) or entry.get("is_live"):
                continue
            item: dict[str, Any] = {
                "id": video_id,
                "youtube_id": video_id,
                "title": str(entry.get("title") or video_id)[:500],
                "source_url": f"https://www.youtube.com/watch?v={video_id}",
                "artist": str(entry.get("artist") or entry.get("uploader") or "")[:200],
            }
            if isinstance(entry.get("duration"), (int, float)):
                item["duration"] = entry["duration"]
            output.append(item)
            if len(output) == 10:
                break
        return output
