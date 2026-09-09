"""Tests for the Music Playback layer (REQ-MUS-001, 002, 004, 005, 006).

These exercise the queue, the controller's command logic and preconditions, and
the producer's pause/resume gate. The Arbiter, transcoder, and resolver are
faked, so no yt-dlp, ffmpeg, or Telegram surface is touched.
"""

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


def _resolved(title: str) -> ResolvedTrack:
    track = Track(title=title, query=title)
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
    """Records acquire/release and hands out a FakeSink."""

    def __init__(self) -> None:
        self.acquired: list[object] = []
        self.released: list[object] = []
        self.sink = FakeSink()

    async def acquire(self, producer: object) -> None:
        self.acquired.append(producer)

    async def release(self, producer: object) -> None:
        self.released.append(producer)

    def handle_for(self, producer: object) -> FakeSink:
        return self.sink


class EmptyTranscoder:
    """Yields nothing, so a track completes immediately."""

    async def stream(self, url: str) -> AsyncIterator[bytes]:
        return
        yield b""  # pragma: no cover - makes this an async generator

    async def stop(self) -> None:
        pass


class BlockingTranscoder:
    """Yields one tiny chunk then blocks, so the track stays 'playing'."""

    def __init__(self) -> None:
        self._release = asyncio.Event()

    async def stream(self, url: str) -> AsyncIterator[bytes]:
        yield b"\x00\x00\x00\x00"
        await self._release.wait()

    async def stop(self) -> None:
        self._release.set()


class FakeResolver:
    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail
        self.calls: list[str] = []

    async def resolve(self, request: str) -> ResolvedTrack:
        self.calls.append(request)
        if self._fail:
            raise TrackNotFoundError("no match")
        return _resolved(request)


def _connected_sessions() -> CallSessionManager:
    sessions = CallSessionManager()
    sessions.begin_join("chat")
    sessions.mark_connected()
    return sessions


# --- TrackQueue -----------------------------------------------------------
def test_queue_enqueue_positions_and_pop_order() -> None:
    q = TrackQueue()
    assert q.enqueue(_resolved("a")) == 1
    assert q.enqueue(_resolved("b")) == 2
    assert [r.track.title for r in q.pending] == ["a", "b"]
    assert q.pop_next().track.title == "a"
    assert q.pop_next().track.title == "b"
    assert q.pop_next() is None


def test_queue_clear() -> None:
    q = TrackQueue()
    q.enqueue(_resolved("a"))
    q.clear()
    assert len(q) == 0


# --- MusicController preconditions (REQ-MUS-002) --------------------------
async def test_play_requires_active_session() -> None:
    controller = MusicController(
        CallSessionManager(), FakeArbiter(), FakeResolver(), EmptyTranscoder()
    )
    reply = await controller.play("a song")
    assert "Join a voice chat first" in reply


async def test_play_requires_connected_session() -> None:
    sessions = CallSessionManager()
    sessions.begin_join("chat")  # connecting, not connected
    controller = MusicController(sessions, FakeArbiter(), FakeResolver(), EmptyTranscoder())
    reply = await controller.play("a song")
    assert "not ready" in reply


async def test_play_not_found_leaves_playback_unchanged() -> None:
    controller = MusicController(
        _connected_sessions(), FakeArbiter(), FakeResolver(fail=True), EmptyTranscoder()
    )
    reply = await controller.play("missing")
    assert "No result found" in reply
    assert not controller.is_active


# --- play / enqueue (REQ-MUS-001) -----------------------------------------
async def test_play_starts_when_idle_then_enqueues() -> None:
    arbiter = FakeArbiter()
    controller = MusicController(
        _connected_sessions(), arbiter, FakeResolver(), BlockingTranscoder()
    )
    first = await controller.play("first")
    assert "Now playing: first" in first
    assert controller.is_active
    assert arbiter.acquired  # output was requested through the Arbiter
    second = await controller.play("second")
    assert "Queued at #1: second" in second
    await controller.stop()


# --- transport controls (REQ-MUS-005) ------------------------------------
async def test_controls_report_when_nothing_playing() -> None:
    controller = MusicController(
        _connected_sessions(), FakeArbiter(), FakeResolver(), EmptyTranscoder()
    )
    for reply in (
        await controller.skip(),
        await controller.pause(),
        await controller.resume(),
        await controller.stop(),
    ):
        assert "Nothing is playing" in reply


async def test_pause_resume_toggles_producer() -> None:
    controller = MusicController(
        _connected_sessions(), FakeArbiter(), FakeResolver(), BlockingTranscoder()
    )
    await controller.play("song")
    assert "Paused" in await controller.pause()
    assert "Already paused" in await controller.pause()
    assert "Resumed" in await controller.resume()
    assert "Already playing" in await controller.resume()
    await controller.stop()


async def test_stop_clears_queue_and_returns_to_silence() -> None:
    arbiter = FakeArbiter()
    controller = MusicController(
        _connected_sessions(), arbiter, FakeResolver(), BlockingTranscoder()
    )
    await controller.play("a")
    await controller.play("b")  # queued
    reply = await controller.stop()
    assert "Stopped music" in reply
    assert not controller.is_active
    assert arbiter.released  # output was released back to the Arbiter


# --- queue view (REQ-MUS-004.3) -------------------------------------------
async def test_show_queue_lists_playing_and_pending() -> None:
    controller = MusicController(
        _connected_sessions(), FakeArbiter(), FakeResolver(), BlockingTranscoder()
    )
    await controller.play("one")
    await controller.play("two")
    view = await controller.show_queue()
    assert "Now playing: one" in view
    assert "two" in view
    await controller.stop()


# --- MusicProducer gate ---------------------------------------------------
async def test_producer_pause_gate_and_stop() -> None:
    arbiter = FakeArbiter()
    transcoder = BlockingTranscoder()
    producer = MusicProducer(arbiter, transcoder)
    await producer.play(_resolved("x"))
    assert producer.is_playing
    await producer.pause()
    assert producer.is_paused
    await producer.resume()
    assert not producer.is_paused
    await producer.stop()
    assert not producer.is_playing
    assert arbiter.released


async def test_producer_completion_fires_callback() -> None:
    finished: list[str] = []

    async def on_finished(prod: MusicProducer, track: ResolvedTrack, ok: bool) -> None:
        finished.append(track.track.title)

    producer = MusicProducer(FakeArbiter(), EmptyTranscoder(), on_finished=on_finished)
    await producer.play(_resolved("done"))
    # Let the empty stream drain and the completion callback run.
    for _ in range(5):
        await asyncio.sleep(0)
    assert finished == ["done"]


if __name__ == "__main__":
    pytest.main([__file__])
