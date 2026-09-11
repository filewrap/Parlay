"""Cookieless media search and stream resolution via public front-ends.

Public YouTube front-ends (Invidious and Piped) expose plain HTTP JSON APIs that
return video metadata and direct audio-stream URLs without cookies, sign-in, or
proof-of-origin tokens. This is the primary, cookieless path for Source
Resolution: it never touches yt-dlp and does not depend on the requesting IP's
standing with YouTube's bot checks, only on a reachable public instance.

Each call tries the configured instances in order and rotates past any that are
unreachable, blocked, or return no usable data, so a single instance outage does
not stop playback. All network calls run in a worker thread with a short timeout
so the event loop is never blocked.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .track import MediaSource, StreamSource

log = logging.getLogger(__name__)

_TIMEOUT_S = 8.0
_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) parlay/0.2"

# Public instances, tried in order with rotation on failure. Overridable so the
# operator can point at self-hosted or nearer instances for latency/reliability.
DEFAULT_INVIDIOUS: tuple[str, ...] = (
    "https://yewtu.be",
    "https://invidious.nerdvpn.de",
    "https://inv.nadeko.net",
)
DEFAULT_PIPED: tuple[str, ...] = (
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.adminforge.de",
    "https://api.piped.yt",
)


def _to_kbps(value: Any) -> float:
    """Normalise a bitrate field (bits/sec, int or string) to kbps for ranking."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return -1.0
    return number / 1000.0 if number > 0 else -1.0


def _video_id_from_watch(path: str) -> str | None:
    """Pull an 11-char video id from a '/watch?v=ID' style Piped url."""
    query = urllib.parse.urlsplit(path).query
    candidate = urllib.parse.parse_qs(query).get("v", [""])[0]
    return candidate if len(candidate) == 11 else None


class PublicSourceClient:
    """Search and resolve audio streams over Invidious and Piped HTTP APIs."""

    def __init__(
        self,
        *,
        invidious: tuple[str, ...] = DEFAULT_INVIDIOUS,
        piped: tuple[str, ...] = DEFAULT_PIPED,
        timeout_s: float = _TIMEOUT_S,
    ) -> None:
        self._invidious = tuple(base.rstrip("/") for base in invidious)
        self._piped = tuple(base.rstrip("/") for base in piped)
        self._timeout_s = timeout_s

    async def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        """Return up to ``limit`` search results, or an empty list if none resolve."""
        return await asyncio.to_thread(self._search_sync, query, limit)

    async def resolve_stream(self, video_id: str) -> StreamSource | None:
        """Return a direct audio StreamSource for a video id, or None on failure."""
        return await asyncio.to_thread(self._resolve_sync, video_id)

    # -- synchronous workers (executed in a thread) --

    def _search_sync(self, query: str, limit: int) -> list[dict[str, Any]]:
        for base in self._invidious:
            try:
                items = self._invidious_search(base, query, limit)
            except Exception as exc:  # unreachable/blocked: rotate to the next
                log.warning("invidious search failed at %s: %s", base, exc)
                continue
            if items:
                return items
        for base in self._piped:
            try:
                items = self._piped_search(base, query, limit)
            except Exception as exc:
                log.warning("piped search failed at %s: %s", base, exc)
                continue
            if items:
                return items
        return []

    def _resolve_sync(self, video_id: str) -> StreamSource | None:
        for base in self._invidious:
            try:
                stream = self._invidious_stream(base, video_id)
            except Exception as exc:
                log.warning("invidious stream failed at %s: %s", base, exc)
                continue
            if stream is not None:
                return stream
        for base in self._piped:
            try:
                stream = self._piped_stream(base, video_id)
            except Exception as exc:
                log.warning("piped stream failed at %s: %s", base, exc)
                continue
            if stream is not None:
                return stream
        return None

    def _get_json(self, url: str) -> Any:
        request = urllib.request.Request(
            url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=self._timeout_s) as response:
            return json.loads(response.read().decode("utf-8"))

    def _invidious_search(self, base: str, query: str, limit: int) -> list[dict[str, Any]]:
        params = urllib.parse.urlencode({"q": query, "type": "video"})
        data = self._get_json(f"{base}/api/v1/search?{params}")
        items: list[dict[str, Any]] = []
        for entry in data if isinstance(data, list) else []:
            video_id = str(entry.get("videoId") or "")
            if len(video_id) != 11 or entry.get("liveNow"):
                continue
            items.append(
                _item(video_id, entry.get("title"), entry.get("author"), entry.get("lengthSeconds"))
            )
            if len(items) >= limit:
                break
        return items

    def _invidious_stream(self, base: str, video_id: str) -> StreamSource | None:
        data = self._get_json(f"{base}/api/v1/videos/{video_id}")
        best_url: str | None = None
        best_abr = -1.0
        for fmt in data.get("adaptiveFormats") or []:
            if not str(fmt.get("type") or "").startswith("audio/"):
                continue
            url = fmt.get("url")
            if not url:
                continue
            abr = _to_kbps(fmt.get("bitrate"))
            if abr > best_abr:
                best_url, best_abr = url, abr
        if not best_url:
            return None
        return StreamSource(
            source=MediaSource.INVIDIOUS, stream_url=best_url, abr=best_abr if best_abr >= 0 else None
        )

    def _piped_search(self, base: str, query: str, limit: int) -> list[dict[str, Any]]:
        params = urllib.parse.urlencode({"q": query, "filter": "videos"})
        data = self._get_json(f"{base}/search?{params}")
        items: list[dict[str, Any]] = []
        for entry in data.get("items") or []:
            video_id = _video_id_from_watch(str(entry.get("url") or ""))
            if not video_id or entry.get("isShort"):
                continue
            duration = entry.get("duration")
            items.append(
                _item(video_id, entry.get("title"), entry.get("uploaderName"), duration)
            )
            if len(items) >= limit:
                break
        return items

    def _piped_stream(self, base: str, video_id: str) -> StreamSource | None:
        data = self._get_json(f"{base}/streams/{video_id}")
        best_url: str | None = None
        best_abr = -1.0
        for stream in data.get("audioStreams") or []:
            url = stream.get("url")
            if not url:
                continue
            abr = _to_kbps(stream.get("bitrate"))
            if abr > best_abr:
                best_url, best_abr = url, abr
        if not best_url:
            return None
        return StreamSource(
            source=MediaSource.PIPED, stream_url=best_url, abr=best_abr if best_abr >= 0 else None
        )


def _item(video_id: str, title: Any, artist: Any, duration: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": video_id,
        "youtube_id": video_id,
        "title": str(title or video_id)[:500],
        "source_url": f"https://www.youtube.com/watch?v={video_id}",
        "artist": str(artist or "")[:200],
    }
    if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration > 0:
        item["duration"] = duration
    return item
