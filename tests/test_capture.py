"""Tests for AudioCaptureService fan-out and lifecycle."""

from __future__ import annotations

import asyncio

import pytest

from parlay.audio.capture import AudioCaptureService
from parlay.audio.frames import CALL_CHANNELS, CALL_RATE


def _frame_bytes(ms: int = 10) -> bytes:
    return b"\x01\x00" * (CALL_RATE * ms // 1000 * CALL_CHANNELS)


@pytest.mark.asyncio
async def test_delivers_resampled_chunks_to_consumer() -> None:
    svc = AudioCaptureService()
    received: list = []

    async def consumer(chunk):
        received.append(chunk)

    svc.registry.subscribe(consumer, rate=16000, channels=1)
    svc.start(asyncio.get_event_loop())
    for _ in range(3):
        svc.on_recorded_data(_frame_bytes(), len(_frame_bytes()))
    await asyncio.sleep(0.1)
    await svc.stop()

    assert len(received) == 3
    assert received[0].rate == 16000
    assert received[0].channels == 1


@pytest.mark.asyncio
async def test_discards_when_no_consumer() -> None:
    svc = AudioCaptureService()
    svc.start(asyncio.get_event_loop())
    for _ in range(5):
        svc.on_recorded_data(_frame_bytes(), len(_frame_bytes()))
    await asyncio.sleep(0.05)
    await svc.stop()
    # Nothing to assert on a consumer; the point is it does not raise or grow.
    assert len(svc.registry) == 0


@pytest.mark.asyncio
async def test_unsubscribe_stops_delivery() -> None:
    svc = AudioCaptureService()
    received: list = []

    async def consumer(chunk):
        received.append(chunk)

    token = svc.registry.subscribe(consumer, rate=48000, channels=2)
    svc.start(asyncio.get_event_loop())
    svc.on_recorded_data(_frame_bytes(), len(_frame_bytes()))
    await asyncio.sleep(0.05)
    count_after_one = len(received)
    svc.registry.unsubscribe(token)
    svc.on_recorded_data(_frame_bytes(), len(_frame_bytes()))
    await asyncio.sleep(0.05)
    await svc.stop()

    assert count_after_one == 1
    assert len(received) == 1  # no delivery after unsubscribe


@pytest.mark.asyncio
async def test_ignores_frames_when_not_running() -> None:
    svc = AudioCaptureService()
    # Not started: on_recorded_data must be a no-op, not raise.
    svc.on_recorded_data(_frame_bytes(), len(_frame_bytes()))
    assert True
