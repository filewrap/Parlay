from __future__ import annotations

import asyncio
import socket
from types import SimpleNamespace

import pytest

import parlay.runtime as runtime_module
from parlay.media.track import TrackNotFoundError
from parlay.runtime import (
    RuntimeCapacityError,
    RuntimeRegistry,
    UnsafeMediaSourceError,
    _validate_media_request,
)


class FakeBridge:
    starts = 0

    def __init__(self, client: object, on_disconnect=None) -> None:
        self.active = False
        self.on_disconnect = on_disconnect
        self.stops = 0

    async def start(self, chat: object) -> None:
        type(self).starts += 1
        await asyncio.sleep(0)
        self.active = True

    async def stop(self) -> None:
        self.stops += 1
        self.active = False

    def interrupt(self) -> None:
        pass

    def play_chunk(self, chunk: object) -> None:
        pass

    def play_frame(self, frame: object) -> None:
        pass


class FakeTranscoder:
    async def stop(self) -> None:
        pass


@pytest.fixture(autouse=True)
def fake_runtime_bridge(monkeypatch, tmp_path):
    FakeBridge.starts = 0
    monkeypatch.setattr(runtime_module, "RawAudioBridge", FakeBridge)
    monkeypatch.setattr(runtime_module, "SourceSelector", lambda token: object())
    monkeypatch.setattr(runtime_module, "TrackResolver", lambda selector: object())
    monkeypatch.setattr(runtime_module, "MediaTranscoder", FakeTranscoder)


@pytest.fixture
def config(tmp_path):
    return SimpleNamespace(
        max_concurrent_calls=2,
        pot_provider_url="",
        activity_db_path=str(tmp_path / "activity.sqlite3"),
    )


async def test_two_runtimes_are_isolated_and_one_can_stop(config) -> None:
    closed = []

    async def on_closed(chat_id, reason):
        closed.append((chat_id, reason))

    registry = RuntimeRegistry(object(), config, on_closed=on_closed)
    first, second = await asyncio.gather(registry.join(1), registry.join(2))
    assert first.bridge is not second.bridge
    assert first.sessions is not second.sessions
    await registry.leave(1)
    await asyncio.sleep(0)
    assert registry.get(1) is None
    assert registry.get(2) is second
    assert second.bridge.active
    assert closed == [(1, "left")]
    await registry.close()


async def test_capacity_reservation_is_atomic(config) -> None:
    registry = RuntimeRegistry(object(), config)
    await asyncio.gather(registry.join(1), registry.join(2))
    with pytest.raises(RuntimeCapacityError):
        await registry.join(3)
    await registry.close()


async def test_same_chat_concurrent_join_builds_once(config) -> None:
    registry = RuntimeRegistry(object(), config)
    one, two = await asyncio.gather(registry.join(10), registry.join(10))
    assert one is two
    assert FakeBridge.starts == 1
    await registry.close()


async def test_stale_disconnect_does_not_close_replacement(config) -> None:
    registry = RuntimeRegistry(object(), config)
    old = await registry.join(7)
    stale = old.bridge.on_disconnect
    await registry.leave(7)
    replacement = await registry.join(7)
    await stale()
    assert registry.get(7) is replacement
    assert replacement.bridge.active
    await registry.close()


async def test_transport_callbacks_are_outside_lifecycle(config) -> None:
    events = []
    registry = None

    async def on_transport(chat_id, connected):
        events.append((chat_id, connected, registry.get(chat_id) is not None))

    registry = RuntimeRegistry(object(), config, on_transport=on_transport)
    await registry.join(4)
    await asyncio.sleep(0)
    await registry.leave(4)
    await asyncio.sleep(0)
    assert events == [(4, True, True), (4, False, False)]


async def test_call_id_is_parent_bound_actual_telegram_id(config) -> None:
    registry = RuntimeRegistry(object(), config)
    runtime = await registry.join(5)
    assert runtime.call_id is None
    runtime.bind_call_id(987654321)
    runtime.bind_call_id(987654321)
    assert runtime.call_id == 987654321
    with pytest.raises(RuntimeError):
        runtime.bind_call_id(123)
    with pytest.raises(ValueError):
        runtime.bind_call_id(0)
    await registry.close()


async def test_leave_removes_runtime_and_finishes_cleanup_after_failure(config) -> None:
    events = []

    class FailingAi:
        async def disengage(self):
            events.append("ai")
            raise RuntimeError("AI cleanup failed")

    async def failing_music():
        events.append("music")
        raise RuntimeError("music cleanup failed")

    async def on_closed(chat_id, reason):
        events.append(("closed", registry.get(chat_id), reason))

    registry = RuntimeRegistry(object(), config, on_closed=on_closed)
    runtime = await registry.join(6)
    runtime.ai = FailingAi()
    runtime.music.on_session_end = failing_music
    with pytest.raises(RuntimeError, match="AI cleanup failed"):
        await registry.leave(6, "failed")
    await asyncio.sleep(0)
    assert registry.get(6) is None
    assert events[:2] == ["ai", "music"]
    assert runtime.bridge.stops == 1
    assert not runtime.sessions.active
    assert events[-1] == ("closed", None, "failed")


async def test_close_waits_for_terminal_callbacks(config) -> None:
    released = asyncio.Event()

    async def on_closed(chat_id, reason):
        await asyncio.sleep(0)
        released.set()

    registry = RuntimeRegistry(object(), config, on_closed=on_closed)
    await registry.join(8)
    await registry.close()
    assert released.is_set()


async def test_checked_play_propagates_resolution_failure(config) -> None:
    class MissingResolver:
        async def resolve(self, request):
            raise TrackNotFoundError("missing")

    registry = RuntimeRegistry(object(), config)
    runtime = await registry.join(9)
    runtime.music._resolver = MissingResolver()
    with pytest.raises(TrackNotFoundError, match="missing"):
        await runtime.command("play", "song")
    assert runtime.music.snapshot()["status"] == "idle"
    await registry.close()


async def test_direct_urls_require_public_https_youtube(monkeypatch) -> None:
    loop = asyncio.get_running_loop()

    async def fake_getaddrinfo(host, port, type=0):
        assert type == socket.SOCK_STREAM
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("142.250.1.1", 443))]

    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
    await _validate_media_request("search words")
    await _validate_media_request("https://www.youtube.com/watch?v=abcdefghijk")
    with pytest.raises(UnsafeMediaSourceError):
        await _validate_media_request("http://youtube.com/watch?v=abcdefghijk")
    with pytest.raises(UnsafeMediaSourceError):
        await _validate_media_request("https://example.com/media.mp3")
