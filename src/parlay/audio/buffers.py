"""Bounded buffers isolating the native audio thread from the asyncio side.

Both ntgcalls callbacks run on a native audio thread every 10 ms and must not
block. These buffers are the hand-off: fixed maximum duration, thread-safe,
non-blocking on the callback side (blueprint ADR-001).

The PlaybackBuffer can run as a plain bounded byte queue (legacy behaviour,
``prebuffer_ms=0``) or as a jitter buffer (``prebuffer_ms>0``). The jitter buffer
delivers provider audio smoothly even when it arrives in bursts: it holds a small
cushion before it starts draining, drains at the pump's steady rate, and re-arms
(waits to refill the cushion) on an underrun instead of dribbling silence into
the middle of a word. It never drops from the front on jitter; audio is discarded
only at a high safety ceiling, so nothing is lost under normal conditions.
"""

from __future__ import annotations

import threading
from collections import deque

from .frames import (
    CALL_CHANNELS,
    CALL_RATE,
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

    The outbound callback pulls exactly N bytes each cycle; when it cannot it
    gets silence. Two modes:

    * Plain (``prebuffer_ms=0``): always ready; on a short read it returns what
      it has padded with silence, and enforces the ``max_ms`` cap with the drop
      policy. This is the original behaviour, used where pacing is not needed.
    * Jitter (``prebuffer_ms>0``): holds a cushion of at least ``prebuffer_ms``
      before it begins draining a run of audio. While armed it serves real bytes
      and, on an underrun, disarms and re-fills the cushion (a clean pause) so a
      burst-starved stream never shatters mid-word. ``mark_ready`` forces arming
      so a reply shorter than the cushion still plays out at its turn boundary.
      Overflow never trims the front except at the ``max_ms`` safety ceiling.
    """

    def __init__(
        self,
        max_ms: int = 2000,
        policy: str = OverflowPolicy.DROP_OLDEST,
        prebuffer_ms: int = 0,
    ) -> None:
        per_ms = bytes_per_ms(CALL_RATE, CALL_CHANNELS)
        self._max_bytes = per_ms * max_ms
        self._prebuffer_bytes = per_ms * max(0, prebuffer_ms)
        self._policy = policy
        self._buf = bytearray()
        self._lock = threading.Lock()
        # With no cushion the buffer is always ready to serve; with a cushion it
        # starts disarmed and arms once enough audio has accumulated.
        self._armed = self._prebuffer_bytes == 0

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
            # DROP_OLDEST: append, then trim from the front to the cap. In jitter
            # mode max_bytes is a high safety ceiling, so this only fires in
            # pathological overflow, not on ordinary jitter.
            self._buf.extend(data)
            overflow = len(self._buf) - self._max_bytes
            if overflow > 0:
                del self._buf[:overflow]

    def mark_ready(self) -> None:
        """Arm the jitter buffer now, releasing whatever is buffered.

        Used at a turn boundary so a reply shorter than the cushion still plays.
        No-op in plain mode.
        """
        with self._lock:
            if self._prebuffer_bytes:
                self._armed = True

    def take(self, n: int) -> bytes:
        """Return exactly n bytes; silence while priming or on an underrun."""
        if n <= 0:
            return b""
        with self._lock:
            if self._prebuffer_bytes:
                if not self._armed:
                    if len(self._buf) >= self._prebuffer_bytes:
                        self._armed = True
                    else:
                        return b"\x00" * n
                if len(self._buf) < n:
                    # Underrun: pause and re-fill the cushion rather than
                    # chopping the current word. The buffered tail is kept.
                    self._armed = False
                    return b"\x00" * n
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
            # Re-prime after a flush so the next run starts with a full cushion.
            self._armed = self._prebuffer_bytes == 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._buf)


def silence(n: int) -> bytes:
    """n bytes of S16LE silence."""
    return b"\x00" * max(0, n)


__all__ = ["CaptureQueue", "OverflowPolicy", "PlaybackBuffer", "silence"]
