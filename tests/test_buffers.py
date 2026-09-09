"""Tests for bounded buffers: capture queue and playback buffer."""

from __future__ import annotations

from parlay.audio.buffers import CaptureQueue, OverflowPolicy, PlaybackBuffer
from parlay.audio.frames import CALL_CHANNELS, CALL_RATE, Pcm48kFrame, bytes_per_ms


def _frame(ms: int) -> Pcm48kFrame:
    return Pcm48kFrame(pcm=b"\x01\x00" * (CALL_RATE * ms // 1000 * CALL_CHANNELS))


def test_capture_queue_drops_oldest_on_overflow() -> None:
    q = CaptureQueue(max_ms=30)  # room for 3 frames of 10 ms
    for _ in range(5):
        q.push(_frame(10))
    assert len(q) == 3
    assert q.dropped == 2


def test_capture_queue_fifo_order() -> None:
    q = CaptureQueue(max_ms=100)
    a, b = Pcm48kFrame(pcm=b"\x01\x00"), Pcm48kFrame(pcm=b"\x02\x00")
    q.push(a)
    q.push(b)
    assert q.pop() is a
    assert q.pop() is b
    assert q.pop() is None


def test_playback_take_pads_with_silence_when_empty() -> None:
    pb = PlaybackBuffer()
    out = pb.take(960)
    assert out == b"\x00" * 960


def test_playback_take_returns_exact_bytes() -> None:
    pb = PlaybackBuffer()
    pb.enqueue(Pcm48kFrame(pcm=b"\xAA\xBB" * 500))  # 1000 bytes
    out = pb.take(400)
    assert len(out) == 400
    # Remaining 600 bytes then padded.
    rest = pb.take(800)
    assert len(rest) == 800
    assert rest.endswith(b"\x00")


def test_playback_drop_oldest_keeps_within_cap() -> None:
    pb = PlaybackBuffer(max_ms=20, policy=OverflowPolicy.DROP_OLDEST)
    cap = bytes_per_ms(CALL_RATE, CALL_CHANNELS) * 20
    pb.enqueue(_frame(15))
    pb.enqueue(_frame(15))  # total 30 ms > 20 ms cap
    assert len(pb) == cap


def test_playback_drop_new_keeps_within_cap() -> None:
    pb = PlaybackBuffer(max_ms=20, policy=OverflowPolicy.DROP_NEW)
    cap = bytes_per_ms(CALL_RATE, CALL_CHANNELS) * 20
    pb.enqueue(_frame(15))
    pb.enqueue(_frame(15))
    assert len(pb) == cap


def test_playback_flush_clears() -> None:
    pb = PlaybackBuffer()
    pb.enqueue(_frame(10))
    assert len(pb) > 0
    pb.flush()
    assert len(pb) == 0
