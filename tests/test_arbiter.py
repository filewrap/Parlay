"""Tests for the Audio Output Arbiter (REQ-INJ-006).

The Arbiter grants the single call output to one producer at a time, pausing the
holder on handover, flushing the buffer so audio never mixes, and resuming the
paused producer when the new holder releases.
"""

from __future__ import annotations

import pytest

from parlay.audio.arbiter import AudioOutputArbiter
from parlay.audio.frames import AudioChunk


class FakeTarget:
    """Records the ordered stream of operations on the call output."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def play_chunk(self, chunk: AudioChunk) -> None:
        self.events.append("chunk")

    def play_frame(self, frame: object) -> None:
        self.events.append("frame")

    def interrupt(self) -> None:
        self.events.append("flush")


class FakeProducer:
    def __init__(self) -> None:
        self.paused = 0
        self.resumed = 0

    async def pause(self) -> None:
        self.paused += 1

    async def resume(self) -> None:
        self.resumed += 1


def _chunk() -> AudioChunk:
    return AudioChunk(pcm=b"\x00\x01", rate=24000, channels=1)


async def test_grants_output_to_single_producer() -> None:
    # AC-INJ-006.1: at most one producer holds the output.
    target = FakeTarget()
    arbiter = AudioOutputArbiter(target)
    a = FakeProducer()
    await arbiter.acquire(a)
    assert arbiter.holder is a


async def test_only_holder_audio_reaches_target() -> None:
    # A non-holder's audio is dropped; the holder's audio passes through.
    target = FakeTarget()
    arbiter = AudioOutputArbiter(target)
    a, b = FakeProducer(), FakeProducer()
    await arbiter.acquire(a)
    arbiter.play_chunk(b, _chunk())  # b does not hold: dropped
    arbiter.play_chunk(a, _chunk())  # a holds: passes
    assert target.events == ["flush", "chunk"]  # acquire flushed once, then a's chunk


async def test_handover_pauses_holder_and_flushes() -> None:
    # AC-INJ-006.2/.4: preempting pauses the holder, records it, and flushes.
    target = FakeTarget()
    arbiter = AudioOutputArbiter(target)
    a, b = FakeProducer(), FakeProducer()
    await arbiter.acquire(a)
    target.events.clear()
    await arbiter.acquire(b)
    assert a.paused == 1
    assert arbiter.holder is b
    assert "flush" in target.events


async def test_release_resumes_paused_producer() -> None:
    # AC-INJ-006.3: releasing the holder resumes the remembered paused producer.
    target = FakeTarget()
    arbiter = AudioOutputArbiter(target)
    a, b = FakeProducer(), FakeProducer()
    await arbiter.acquire(a)
    await arbiter.acquire(b)
    await arbiter.release(b)
    assert a.resumed == 1
    assert arbiter.holder is a


async def test_release_without_paused_goes_idle() -> None:
    # AC-INJ-006.5: with no producer holding, output is idle (buffer -> silence).
    target = FakeTarget()
    arbiter = AudioOutputArbiter(target)
    a = FakeProducer()
    await arbiter.acquire(a)
    await arbiter.release(a)
    assert arbiter.holder is None
    # A released producer can no longer push audio.
    target.events.clear()
    arbiter.play_chunk(a, _chunk())
    assert target.events == []


async def test_reacquire_by_holder_is_noop() -> None:
    target = FakeTarget()
    arbiter = AudioOutputArbiter(target)
    a = FakeProducer()
    await arbiter.acquire(a)
    await arbiter.acquire(a)
    assert a.paused == 0
    assert arbiter.holder is a


async def test_handle_gates_by_producer() -> None:
    target = FakeTarget()
    arbiter = AudioOutputArbiter(target)
    a = FakeProducer()
    handle = arbiter.handle_for(a)
    handle.play_chunk(_chunk())  # a does not hold yet: dropped
    assert target.events == []
    await arbiter.acquire(a)
    target.events.clear()
    handle.play_chunk(_chunk())
    handle.interrupt()
    assert target.events == ["chunk", "flush"]


if __name__ == "__main__":
    pytest.main([__file__])
