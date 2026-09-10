from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import parlay.runtime as runtime_module
from parlay.runtime import RuntimeCapacityError, RuntimeRegistry


class FakeBridge:
    starts = 0

    def __init__(self, client: object, on_disconnect=None) -> None:
        self.active = False
        self.on_disconnect = on_disconnect

    async def start(self, chat: object) -> None:
        type(self).starts += 1
        await asyncio.sleep(0)
        self.active = True

    async def stop(self) -> None:
        self.active = False

    def interrupt(self) -> None:
        pass

    def play_chunk(self, chunk: object) -> None:
        pass

    def play_frame(self, frame: object) -> None:
        pass


@pytest.fixture(autouse=True)
def fake_runtime_bridge(monkeypatch, tmp_path):
    FakeBridge.starts = 0
    monkeypatch.setattr(runtime_module, "RawAudioBridge", FakeBridge)
    monkeypatch.setattr(runtime_module, "SourceSelector", lambda token: object())
    monkeypatch.setattr(runtime_module, "TrackResolver", lambda selector: object())
    monkeypatch.setattr(runtime_module, "MediaTranscoder", lambda: object())


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
