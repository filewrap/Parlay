"""Official YouTube chart candidate collection."""

from __future__ import annotations

import asyncio
import json
import urllib.parse
import urllib.request
from typing import Any


class YouTubeChartCollector:
    """Collect up to 100 current Music chart videos using videos.list."""

    ENDPOINT = "https://www.googleapis.com/youtube/v3/videos"

    def __init__(self, api_key: str | None, region: str = "US") -> None:
        self.api_key = api_key
        self.region = region

    async def collect(self, limit: int = 100) -> list[dict[str, Any]]:
        if not self.api_key:
            return []
        return await asyncio.to_thread(self._collect_sync, min(100, max(1, limit)))

    def _collect_sync(self, limit: int) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        token: str | None = None
        while len(results) < limit:
            parameters = {
                "part": "snippet",
                "chart": "mostPopular",
                "videoCategoryId": "10",
                "regionCode": self.region,
                "maxResults": "50",
                "key": self.api_key or "",
            }
            if token:
                parameters["pageToken"] = token
            request = urllib.request.Request(
                f"{self.ENDPOINT}?{urllib.parse.urlencode(parameters)}",
                headers={"Accept": "application/json", "User-Agent": "Parlay-Compass/1"},
            )
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.load(response)
            for item in payload.get("items", []):
                video_id = item.get("id")
                snippet = item.get("snippet", {})
                if not video_id or any(row["track_id"] == video_id for row in results):
                    continue
                results.append(
                    {
                        "track_id": video_id,
                        "title": snippet.get("title", "Untitled"),
                        "artist": snippet.get("channelTitle", ""),
                        "source_url": f"https://www.youtube.com/watch?v={video_id}",
                        "tags": [str(tag) for tag in snippet.get("tags", [])[:20]],
                        "source": "youtube_data_api_v3_music_chart",
                        "rights_note": "Metadata from the official YouTube Data API; playback and reuse remain subject to YouTube and rightsholder terms.",
                    }
                )
                if len(results) >= limit:
                    break
            token = payload.get("nextPageToken")
            if not token:
                break
        return results
