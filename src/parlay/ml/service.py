"""Public Compass service contract and hourly orchestration."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .discovery import YouTubeChartCollector
from .model import POSITIVE, HybridRanker
from .store import CompassStore

Delivery = Callable[[str, str, list[dict[str, Any]]], Awaitable[object] | object]


class CompassService:
    """Train, rank, persist, and optionally deliver opt-in recommendations."""

    def __init__(
        self, db_path: str | Path, model_dir: str | Path, youtube_api_key: str | None = None
    ) -> None:
        self.store = CompassStore(db_path)
        self.ranker = HybridRanker(model_dir)
        self.collector = YouTubeChartCollector(youtube_api_key)
        self._deliver: Delivery | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()

    async def start(self, deliver: Delivery | None = None) -> None:
        """Start hourly work. Repeated calls do not create duplicate schedulers."""
        if self._task and not self._task.done():
            return
        self._deliver = deliver
        self._stopping.clear()
        self._task = asyncio.create_task(self._scheduler(), name="compass-hourly")

    async def stop(self) -> None:
        """Stop the scheduler without creating catch-up deliveries."""
        self._stopping.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def set_preferences(
        self,
        user_id: str,
        enabled: bool,
        count: int = 10,
        quiet_start: int | None = None,
        quiet_end: int | None = None,
        timezone: str = "UTC",
    ) -> None:
        user_id = self._required("user_id", user_id)
        if isinstance(count, bool) or not 1 <= count <= 10:
            raise ValueError("count must be an integer from 1 to 10")
        for name, hour in (("quiet_start", quiet_start), ("quiet_end", quiet_end)):
            if hour is not None and (isinstance(hour, bool) or not 0 <= hour <= 23):
                raise ValueError(f"{name} must be an integer hour from 0 to 23")
        if (quiet_start is None) != (quiet_end is None):
            raise ValueError("quiet_start and quiet_end must be set together")
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as error:
            raise ValueError("timezone must be a valid IANA timezone, such as UTC") from error
        self.store.set_preferences(user_id, bool(enabled), count, quiet_start, quiet_end, timezone)

    def ingest_track(
        self,
        track_id: str,
        title: str,
        artist: str = "",
        source_url: str = "",
        tags: list[str] | None = None,
    ) -> None:
        track_id, title = self._required("track_id", track_id), self._required("title", title)
        if tags is not None and (
            not isinstance(tags, list) or not all(isinstance(x, str) for x in tags)
        ):
            raise TypeError("tags must be a list of strings")
        source = "manual"
        rights = "Caller-supplied metadata; the caller is responsible for source and usage rights."
        self.store.ingest_track(
            track_id, title, artist.strip(), source_url.strip(), tags or [], source, rights
        )

    def record_event(
        self,
        user_id: str,
        track_id: str,
        event_type: str,
        event_id: str,
        context: dict[str, Any] | None = None,
    ) -> bool:
        for name, value in (
            ("user_id", user_id),
            ("track_id", track_id),
            ("event_type", event_type),
            ("event_id", event_id),
        ):
            self._required(name, value)
        if context is not None and not isinstance(context, dict):
            raise TypeError("context must be a dict")
        kind = event_type.strip().lower()
        require_exposure = kind in {"click", "positive", "like", "dislike", "negative"}
        return self.store.record_event(
            user_id, track_id, kind, event_id, context or {}, require_exposure
        )

    def recommend(self, user_id: str, limit: int = 10) -> list[dict[str, Any]]:
        self._required("user_id", user_id)
        if isinstance(limit, bool) or not 1 <= limit <= 10:
            raise ValueError("limit must be an integer from 1 to 10")
        preference = self.store.preference(user_id)
        if not preference or not preference["enabled"] or preference["paused"]:
            return []
        rows = self.store.candidates(user_id)
        if not rows:
            return []
        collaborative = self.ranker.collaborative_scores(user_id, [row["track_id"] for row in rows])
        tracks, events = self.store.training_rows()
        liked = {
            event["track_id"]
            for event in events
            if event["user_id"] == user_id and event["event_type"] in POSITIVE
        }
        liked_tags: set[str] = set()
        for row in tracks:
            if row["track_id"] in liked:
                liked_tags.update(json.loads(row["tags_json"]))
        scored: list[tuple[float, Any, str]] = []
        for position, row in enumerate(rows):
            tags = set(json.loads(row["tags_json"]))
            content = len(tags & liked_tags) / max(1, len(tags | liked_tags))
            popularity = 1.0 / (1.0 + position)
            if collaborative:
                score = (
                    0.75 * collaborative.get(row["track_id"], 0.0)
                    + 0.2 * content
                    + 0.05 * popularity
                )
                reason = "learned listening and feedback fit"
                if content > 0:
                    reason += ", with shared metadata tags"
            else:
                score = 0.8 * content + 0.2 * popularity
                reason = (
                    "cold-start chart fallback" if not liked_tags else "cold-start metadata fit"
                )
            scored.append((score, row, reason))
        selected = sorted(scored, key=lambda value: value[0], reverse=True)[:limit]
        output = [
            {
                "id": row["track_id"],
                "title": row["title"],
                "source_url": row["source_url"],
                "score": float(score),
                "reason": reason,
                "model_version": self.ranker.version,
            }
            for score, row, reason in selected
        ]
        self.store.add_exposures(user_id, [item["id"] for item in output], self.ranker.version)
        return output

    def feedback(self, user_id: str, track_id: str, positive: bool, event_id: str) -> bool:
        """Record one like/dislike only when it binds to an unhandled exposure."""
        return self.record_event(
            user_id,
            track_id,
            "positive" if positive else "dislike",
            event_id,
            {"source": "recommendation_feedback"},
        )

    def train(self) -> dict[str, Any]:
        """Train synchronously. Async integrations must call this in a worker thread."""
        tracks, events = self.store.training_rows()
        return self.ranker.train(tracks, events)

    def reset_user(self, user_id: str) -> None:
        """Clear pause/dislike state and prior exposure suppression, preserving events."""
        self.store.reset_user(self._required("user_id", user_id))

    def delete_user(self, user_id: str) -> None:
        """Delete all persisted preference, event, and exposure data for one user."""
        self.store.delete_user(self._required("user_id", user_id))

    async def _scheduler(self) -> None:
        while not self._stopping.is_set():
            slot = datetime.now(UTC).strftime("%Y-%m-%dT%H")
            if await asyncio.to_thread(self.store.claim_job, "discovery", slot):
                try:
                    for item in await self.collector.collect(100):
                        await asyncio.to_thread(self.store.ingest_track, **item)
                except Exception:
                    pass
            if await asyncio.to_thread(self.store.claim_job, "training", slot):
                try:
                    await asyncio.to_thread(self.train)
                except Exception:
                    pass
            if self._deliver and await asyncio.to_thread(self.store.claim_job, "delivery", slot):
                await self._deliver_hour(slot)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=60.0)
            except TimeoutError:
                continue

    async def _deliver_hour(self, slot: str) -> None:
        for preference in await asyncio.to_thread(self.store.enabled_users):
            if self._quiet(preference):
                continue
            items = await asyncio.to_thread(
                self.recommend, preference["user_id"], preference["recommendation_count"]
            )
            if not items or not self._deliver:
                continue
            result = self._deliver(preference["user_id"], "Your hourly Compass picks", items)
            if inspect.isawaitable(result):
                result = await result
            if result is False or result == "blocked":
                await asyncio.to_thread(
                    self.set_preferences,
                    preference["user_id"],
                    False,
                    preference["recommendation_count"],
                    preference["quiet_start"],
                    preference["quiet_end"],
                    preference["timezone"],
                )

    @staticmethod
    def _quiet(preference: Any) -> bool:
        start, end = preference["quiet_start"], preference["quiet_end"]
        if start is None:
            return False
        hour = datetime.now(ZoneInfo(preference["timezone"])).hour
        return start <= hour < end if start < end else hour >= start or hour < end

    @staticmethod
    def _required(name: str, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
        return value.strip()
