"""Tests for queue, controller snapshots, and the paced music producer."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from parlay.media.resolver import ResolvedTrack
from parlay.media.track import MediaSource, StreamSource, Track, TrackNotFoundError
from parlay.music.controller import MusicController
from parlay.music.producer import MusicProducer
from parlay.music.queue import TrackQueue
from parlay.session import CallSessionManager


def _resolved(title: str, *, url: str | None = None) -> ResolvedTrack:
    track = Track(title=title, query=title, webpage_url=url, duration_s=12.0)
    stream = StreamSource(source=MediaSource.YOUTUBE, stream_url=f"http://s/{title}")
    return ResolvedTrack(track=track, stream=stream)


class FakeSink:
    def __init__(self) -> None:
        self.frames = 0

    def play_chunk(self, chunk: object) -> None:
        self.frames += 1

    def play_frame(self, frame: object) -> None:
        self.frames += 1

    def interrupt(self) -> None:
        pass


class FakeArbiter:
    def __init__(self) -> None:
        self.acquired = []
        self.released = []
        self.sink = FakeSink()

    async def acquire(self, producer: object) -> None:
        self.acquired.append(producer)

    async def release(self, producer: object) -> None:
        self.released.append(producer)

    def handle_for(self, producer: object) -> FakeSink:
        return self.sink


class EmptyTranscoder:
    async def stream(self, url: str) -> AsyncIterator[bytes]:
        return
        yield b""

    async def stop(self) -> None:
        pass


class BlockingTranscoder:
    def __init__(self) -> None:
        self._release = asyncio.Event()

    async def stream(self, url: str) -> AsyncIterator[bytes]:
        yield b"\x00" * 1920
        await self._release.wait()

    async def stop(self) -> None:
        self._release.set()


class FiniteTranscoder:
    async def stream(self, url: str) -> AsyncIterator[bytes]:
        yield b"\x00" * 1920

    async def stop(self) -> None:
        pass


class FakeResolver:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    async def resolve(self, request: str) -> ResolvedTrack:
        if self.fail:
            raise TrackNotFoundError("no match")
        return _resolved(request)


def _connected_sessions() -> CallSessionManager:
    sessions = CallSessionManager()
    sessions.begin_join("chat")
    sessions.mark_connected()
    return sessions


def test_queue_enqueue_positions_and_pop_order() -> None:
    queue = TrackQueue()
    assert queue.enqueue(_resolved("a")) == 1
    assert queue.enqueue(_resolved("b")) == 2
    assert [item.track.title for item in queue.pending] == ["a", "b"]
    assert queue.pop_next().track.title == "a"
    assert queue.pop_next().track.title == "b"
    assert queue.pop_next() is None


def test_queue_clear() -> None:
    queue = TrackQueue()
    queue.enqueue(_resolved("a"))
    queue.clear()
    assert len(queue) == 0


async def test_play_requires_active_and_connected_session() -> None:
    controller = MusicController(CallSessionManager(), FakeArbiter(), FakeResolver(), EmptyTranscoder())
    assert "Join a voice chat first" in await controller.play("song")
    sessions = CallSessionManager()
    sessions.begin_join("chat")
    controller = MusicController(sessions, FakeArbiter(), FakeResolver(), EmptyTranscoder())
    assert "not ready" in await controller.play("song")


async def test_play_not_found_leaves_playback_unchanged() -> None:
    controller = MusicController(
        _connected_sessions(), FakeArbiter(), FakeResolver(fail=True), EmptyTranscoder()
    )
    assert "No result found" in await controller.play("missing")
    assert not controller.is_active


async def test_play_enqueue_pause_resume_stop_snapshots() -> None:
    controller = MusicController(
        _connected_sessions(), FakeArbiter(), FakeResolver(), BlockingTranscoder()
    )
    await controller.play("first")
    await controller.play("second")
    snapshot = controller.snapshot()
    assert snapshot["status"] == "playing"
    assert snapshot["track"]["source_url"] == "first"
    assert [item["title"] for item in snapshot["queue"]] == ["second"]
    await controller.pause()
    assert controller.snapshot()["status"] == "paused"
    await controller.resume()
    assert controller.snapshot()["status"] == "playing"
    await controller.stop()
    assert controller.snapshot()["status"] == "idle"
    assert controller.snapshot()["queue"] == []


async def test_snapshot_uses_public_source_and_youtube_id() -> None:
    class Resolver:
        async def resolve(self, request):
            return _resolved("video", url="https://www.youtube.com/watch?v=abcdefghijk")

    controller = MusicController(
        _connected_sessions(), FakeArbiter(), Resolver(), BlockingTranscoder()
    )
    await controller.play("video")
    track = controller.snapshot()["track"]
    assert track["source_url"] == "https://www.youtube.com/watch?v=abcdefghijk"
    assert track["youtube_id"] == "abcdefghijk"
    assert "http://s/video" not in str(track)
    await controller.stop()


async def test_finite_end_advances_once_and_emits_callback() -> None:
    changes = []

    async def changed(snapshot):
        changes.append(snapshot)

    controller = MusicController(
        _connected_sessions(), FakeArbiter(), FakeResolver(), FiniteTranscoder(), on_change=changed
    )
    await controller.play("one")
    await controller.play("two")
    for _ in range(20):
        await asyncio.sleep(0.01)
        if controller.snapshot()["status"] == "idle":
            break
    await asyncio.sleep(0)
    assert controller.snapshot()["status"] == "idle"
    assert any(snapshot["track"] and snapshot["track"]["title"] == "two" for snapshot in changes)
    assert changes[-1]["status"] == "idle"


async def test_producer_cursor_counts_paced_pcm() -> None:
    producer = MusicProducer(FakeArbiter(), BlockingTranscoder())
    await producer.play(_resolved("x"))
    for _ in range(10):
        await asyncio.sleep(0)
        if producer.position_seconds:
            break
    assert producer.position_seconds == pytest.approx(0.01)
    await producer.stop()


async def test_producer_completion_fires_callback_and_releases() -> None:
    finished = []
    arbiter = FakeArbiter()

    async def on_finished(producer, track, ok):
        finished.append((track.track.title, ok))

    producer = MusicProducer(arbiter, EmptyTranscoder(), on_finished=on_finished)
    await producer.play(_resolved("done"))
    for _ in range(5):
        await asyncio.sleep(0)
    assert finished == [("done", True)]
    assert arbiter.released
