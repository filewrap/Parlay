"""SourceSelector: resolve a streamable URL across fallback Media Sources.

Source Resolution tries the Media Sources in priority order, YouTube (with a
PO token), then Invidious, then Piped, and falls through on any block or
failure. Only when every source fails does it raise SourceResolutionError; the
caller then reports failure without touching the current track (AC-MUS-003.2/.3).
Each source picks the highest-quality audio-only format it offers (AC-MUS-003.4).

yt-dlp is imported lazily and every extraction runs in a thread, so importing
this module never pulls the native/network surface and the event loop is never
blocked. Invidious and Piped are reached by pointing yt-dlp at an instance's
watch URL, so one extractor path serves all three sources.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .po_token import PoTokenError, PoTokenProvider
from .track import (
    SOURCE_PRIORITY,
    MediaSource,
    SourceResolutionError,
    StreamSource,
    Track,
)

log = logging.getLogger(__name__)

# Default public fallback instances; overridable by the caller for tuning.
DEFAULT_INVIDIOUS = "https://yewtu.be"
DEFAULT_PIPED = "https://piped.video"


class SourceSelector:
    """Resolves a Track to a StreamSource by trying sources in priority order."""

    def __init__(
        self,
        po_tokens: PoTokenProvider,
        *,
        invidious_base: str = DEFAULT_INVIDIOUS,
        piped_base: str = DEFAULT_PIPED,
    ) -> None:
        self._po_tokens = po_tokens
        self._invidious_base = invidious_base.rstrip("/")
        self._piped_base = piped_base.rstrip("/")

    async def resolve(self, track: Track) -> StreamSource:
        """Try each Media Source in order, returning the first that resolves."""
        errors: list[str] = []
        for source in SOURCE_PRIORITY:
            try:
                return await self._resolve_one(source, track)
            except Exception as exc:  # block/failure: fall through to next source
                log.warning("source %s failed for %r: %s", source, track.query, exc)
                errors.append(f"{source}: {exc}")
        raise SourceResolutionError(
            f"all media sources failed for {track.query!r}: {'; '.join(errors)}"
        )

    async def _resolve_one(self, source: MediaSource, track: Track) -> StreamSource:
        target = self._target_url(source, track)
        opts = await self._ydl_opts(source)
        info = await asyncio.to_thread(self._extract, target, opts)
        stream_url, abr = self._best_audio(info)
        if not stream_url:
            raise SourceResolutionError(f"{source} offered no audio stream")
        return StreamSource(source=source, stream_url=stream_url, abr=abr)

    def _target_url(self, source: MediaSource, track: Track) -> str:
        ref = track.webpage_url or track.query
        if source is MediaSource.YOUTUBE:
            return ref
        # Point yt-dlp at the fallback instance's watch URL for the same video.
        video_id = _youtube_id(ref)
        base = self._invidious_base if source is MediaSource.INVIDIOUS else self._piped_base
        if video_id:
            return f"{base}/watch?v={video_id}"
        return ref

    async def _ydl_opts(self, source: MediaSource) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "format": "bestaudio/best",
        }
        if source is MediaSource.YOUTUBE:
            # Cookieless YouTube via the mweb/web_music client and a PO token.
            try:
                token = await self._po_tokens.fetch()
            except PoTokenError:
                raise
            opts["extractor_args"] = {
                "youtube": {
                    "player_client": ["mweb", "web_music"],
                    "po_token": [f"mweb.gvs+{token}"],
                }
            }
        return opts

    @staticmethod
    def _extract(target: str, opts: dict[str, Any]) -> dict[str, Any]:
        from yt_dlp import YoutubeDL  # lazy: avoids import at module load

        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(target, download=False)
        if info is None:
            raise SourceResolutionError("extractor returned no info")
        if "entries" in info:  # a playlist/search result: take the first entry
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
            # Some extractors give a single resolved url on the info dict itself.
            return info.get("url"), info.get("abr")
        best = max(candidates, key=lambda f: f.get("abr") or f.get("tbr") or 0.0)
        return best.get("url"), best.get("abr") or best.get("tbr")


def _youtube_id(ref: str) -> str | None:
    """Extract an 11-char YouTube video id from a URL or bare id."""
    import re

    if re.fullmatch(r"[A-Za-z0-9_-]{11}", ref):
        return ref
    match = re.search(r"(?:v=|youtu\.be/|/watch\?v=)([A-Za-z0-9_-]{11})", ref)
    return match.group(1) if match else None
