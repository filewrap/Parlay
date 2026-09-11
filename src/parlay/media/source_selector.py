"""SourceSelector: resolve a streamable URL, cookieless public sources first.

Source Resolution leads with the public front-ends (Invidious, then Piped) via
#PublicSourceClient, which return direct audio URLs over plain HTTP with no
cookies, sign-in, or proof-of-origin tokens and no dependence on the requesting
IP's standing with YouTube's bot checks. Only if every public instance fails
does it fall back to yt-dlp against YouTube using the cookieless tv/web_safari
client, with the PO-token provider attached as a last resort. Each source picks
the highest-bitrate audio-only format it offers (AC-MUS-003.4).

yt-dlp is imported lazily and every extraction runs in a thread, so importing
this module never pulls the native/network surface and the event loop is never
blocked.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from .po_token import PoTokenProvider
from .public_sources import PublicSourceClient, ResolvedStream
from .track import (
    MediaSource,
    SourceResolutionError,
    StreamSource,
    Track,
)

log = logging.getLogger(__name__)

_YOUTUBE_ID_RE = re.compile(r"(?:v=|youtu\.be/|/watch\?v=|/shorts/)([A-Za-z0-9_-]{11})")
_BARE_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")


class SourceSelector:
    """Resolve a Track to a StreamSource, public sources first then yt-dlp."""

    def __init__(
        self,
        po_tokens: PoTokenProvider,
        *,
        public: PublicSourceClient | None = None,
    ) -> None:
        self._po_tokens = po_tokens
        self._public = public or PublicSourceClient()

    @property
    def public(self) -> PublicSourceClient:
        return self._public

    async def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Search public front-ends first; fall back to a cookieless yt-dlp search."""
        items = await self._public.search(query, limit)
        if items:
            return items
        return await asyncio.to_thread(self._ytdlp_search, query, limit)

    async def resolve(self, track: Track) -> StreamSource:
        """Resolve a Track to a StreamSource (public first, then yt-dlp)."""
        video_id = _youtube_id(track.webpage_url or track.query)
        if video_id is not None:
            resolved = await self.resolve_by_id(video_id)
            if resolved is not None:
                return resolved.stream
            raise SourceResolutionError(f"no audio stream for {track.query!r}")
        # Non-YouTube direct link: hand the raw URL to yt-dlp.
        info = await asyncio.to_thread(
            self._extract, track.webpage_url or track.query, self._ydl_opts()
        )
        stream_url, abr = self._best_audio(info)
        if not stream_url:
            raise SourceResolutionError(f"no audio stream for {track.query!r}")
        return StreamSource(source=MediaSource.YOUTUBE, stream_url=stream_url, abr=abr)

    async def resolve_by_id(self, video_id: str) -> ResolvedStream | None:
        """Resolve a YouTube id to a stream: public front-ends first, then yt-dlp."""
        resolved = await self._public.resolve(video_id)
        if resolved is not None:
            return resolved
        try:
            info = await asyncio.to_thread(
                self._extract, f"https://www.youtube.com/watch?v={video_id}", self._ydl_opts()
            )
        except Exception as exc:
            log.warning("yt-dlp fallback failed for %s: %s", video_id, exc)
            return None
        stream_url, abr = self._best_audio(info)
        if not stream_url:
            return None
        stream = StreamSource(source=MediaSource.YOUTUBE, stream_url=stream_url, abr=abr)
        return ResolvedStream(
            stream=stream,
            title=info.get("title"),
            duration_s=info.get("duration"),
        )

    def youtube_options(self) -> dict[str, Any]:
        """Fresh cookieless yt-dlp options shared by search, metadata, and streams."""
        return self._ydl_opts()

    def _ydl_opts(self) -> dict[str, Any]:
        # Cookieless YouTube via the tv/web_safari clients, the least-scrutinised
        # clients that work for anonymous requests from datacenter IPs. The bgutil
        # PO-token provider is attached as a best-effort last resort; it is not
        # required for the public path and does not guarantee access on its own.
        return {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "format": "bestaudio/best",
            "socket_timeout": 10,
            "retries": 1,
            "extractor_retries": 1,
            "noplaylist": True,
            "extractor_args": {
                "youtube": {"player_client": ["tv", "web_safari"]},
                **self._po_tokens.extractor_args(),
            },
        }

    def _ytdlp_search(self, query: str, limit: int) -> list[dict[str, Any]]:
        options = {**self._ydl_opts(), "extract_flat": "in_playlist"}
        info = self._extract(f"ytsearch{limit}:{query}", options, allow_empty=True)
        entries = info.get("entries") if "entries" in info else [info]
        items: list[dict[str, Any]] = []
        for entry in entries or []:
            if not entry:
                continue
            video_id = str(entry.get("id") or "")
            if len(video_id) != 11 or entry.get("is_live"):
                continue
            item: dict[str, Any] = {
                "id": video_id,
                "youtube_id": video_id,
                "title": str(entry.get("title") or video_id)[:500],
                "source_url": f"https://www.youtube.com/watch?v={video_id}",
                "artist": str(entry.get("artist") or entry.get("uploader") or "")[:200],
            }
            duration = entry.get("duration")
            if isinstance(duration, (int, float)) and not isinstance(duration, bool):
                item["duration"] = duration
            items.append(item)
            if len(items) >= limit:
                break
        return items

    @staticmethod
    def _extract(target: str, opts: dict[str, Any], *, allow_empty: bool = False) -> dict[str, Any]:
        from yt_dlp import YoutubeDL  # lazy: avoids import at module load

        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(target, download=False)
        if info is None:
            if allow_empty:
                return {}
            raise SourceResolutionError("extractor returned no info")
        if "entries" in info and not allow_empty:
            entries = [e for e in info["entries"] if e]
            if not entries:
                raise SourceResolutionError("extractor returned no entries")
            info = entries[0]
        return info

    @staticmethod
    def _best_audio(info: dict[str, Any]) -> tuple[str | None, float | None]:
        """Pick the highest-bitrate audio-only format (AC-MUS-003.4)."""
        formats = info.get("formats") or []
        audio_only = [
            f
            for f in formats
            if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")
        ]
        candidates = audio_only or formats
        if not candidates:
            return info.get("url"), info.get("abr")
        best = max(candidates, key=lambda f: f.get("abr") or f.get("tbr") or 0.0)
        return best.get("url"), best.get("abr") or best.get("tbr")


def _youtube_id(ref: str) -> str | None:
    """Extract an 11-char YouTube video id from a URL or bare id."""
    if _BARE_ID_RE.fullmatch(ref):
        return ref
    match = _YOUTUBE_ID_RE.search(ref)
    return match.group(1) if match else None
