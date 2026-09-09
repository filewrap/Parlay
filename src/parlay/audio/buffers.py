"""Bounded buffers isolating the native audio thread from the asyncio side.

Both ntgcalls callbacks run on a native audio thread every 10 ms and must not
block. These buffers are the hand-off: fixed maximum duration, thread-safe,
non-blocking on the callback side (blueprint ADR-001).
"""

from __future__ import annotations

import threading
from collections import deque

from .frames import (
    CALL_CHANNELS,
    CALL_RATE,
    SAMPLE_WIDTH,
    AudioChunk,
    Pcm48kFrame,
    bytes_per_ms,
)


class CaptureQueue:
    """Bounded FIFO of inbound frames; drops oldest on overflow.

    Used between the inbound callback (producer, native thread) and a consumer
    (asyncio side). Never blocks the producer.
    """

    def __init__(self, max_ms: int = 400) -> None:
        frame_ms = 10
        self._maxlen = max(1, max_ms // frame_ms)
        self._dq: deque[Pcm48kFrame] = deque(maxlen=self._maxlen)
        self._lock = threading.Lock()
        self.dropped = 0

    def push(self, frame: Pcm48kFrame) -> None:
        with self._lock:
            if len(self._dq) == self._maxlen:
                self.dropped += 1  # deque will evict the oldest on append
            self._dq.append(frame)

    def pop(self) -> Pcm48kFrame | None:
        with self._lock:
            if not self._dq:
                return None
            return self._dq.popleft()

    def clear(self) -> None:
        with self._lock:
            self._dq.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._dq)


class OverflowPolicy:
    DROP_OLDEST = "drop_oldest"
    DROP_NEW = "drop_new"


class PlaybackBuffer:
    """Bounded byte buffer of 48 kHz stereo PCM waiting to be sent to the call.

    The outbound callback pulls exactly N bytes each cycle; when empty it gets
    silence. Enforces a fixed maximum queued duration with a drop policy, and
    supports immediate flush for interruption.
    """

    def __init__(self, max_ms: int = 2000, policy: str = OverflowPolicy.DROP_OLDEST) -> None:
        self._max_bytes = bytes_per_ms(CALL_RATE, CALL_CHANNELS) * max_ms
        self._policy = policy
        self._buf = bytearray()
        self._lock = threading.Lock()

    def enqueue(self, frame: Pcm48kFrame) -> None:
        data = frame.pcm
        with self._lock:
            if len(self._buf) + len(data) <= self._max_bytes:
                self._buf.extend(data)
                return
            if self._policy == OverflowPolicy.DROP_NEW:
                # Keep what fits, discard the rest of the new data.
                room = self._max_bytes - len(self._buf)
                if room > 0:
                    self._buf.extend(data[:room])
                return
            # DROP_OLDEST: append, then trim from the front to the cap.
            self._buf.extend(data)
            overflow = len(self._buf) - self._max_bytes
            if overflow > 0:
                del self._buf[:overflow]

    def take(self, n: int) -> bytes:
        """Return exactly n bytes, padding with silence when short."""
        if n <= 0:
            return b""
        with self._lock:
            if len(self._buf) >= n:
                out = bytes(self._buf[:n])
                del self._buf[:n]
                return out
            out = bytes(self._buf)
            self._buf.clear()
        return out + b"\x00" * (n - len(out))

    def flush(self) -> None:
        with self._lock:
            self._buf.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._buf)


def silence(n: int) -> bytes:
    """n bytes of S16LE silence."""
    return b"\x00" * max(0, n - (n % SAMPLE_WIDTH)) if False else b"\x00" * n


__all__ = ["CaptureQueue", "PlaybackBuffer", "OverflowPolicy", "silence"]
