"""Audio boundary formats.

The ntgcalls raw path delivers and expects 16-bit little-endian PCM at 48 kHz,
stereo, in 10 ms frames (480 samples/channel -> 960 samples -> 1920 bytes).
Consumers (e.g. Gemini) work at other rates and mono, so audio handed across
the bridge is always rate- and channel-tagged.
"""

from __future__ import annotations

from dataclasses import dataclass

# Call-boundary constants (ntgcalls raw path).
CALL_RATE = 48_000
CALL_CHANNELS = 2
FRAME_MS = 10
SAMPLE_WIDTH = 2  # bytes per sample (S16LE)

# Bytes in one 10 ms stereo frame at 48 kHz: 480 * 2ch * 2 bytes = 1920.
CALL_FRAME_BYTES = (CALL_RATE * FRAME_MS // 1000) * CALL_CHANNELS * SAMPLE_WIDTH


def bytes_per_ms(rate: int, channels: int) -> int:
    """Bytes of S16LE PCM in one millisecond at the given rate/channels."""
    return rate * channels * SAMPLE_WIDTH // 1000


@dataclass(frozen=True)
class AudioChunk:
    """Rate- and channel-tagged S16LE PCM handed to/from consumers."""

    pcm: bytes
    rate: int
    channels: int

    @property
    def duration_ms(self) -> float:
        denom = bytes_per_ms(self.rate, self.channels)
        return len(self.pcm) / denom if denom else 0.0


@dataclass(frozen=True)
class Pcm48kFrame:
    """One unit of S16LE PCM at the 48 kHz stereo call boundary.

    Not necessarily exactly 10 ms: overflow/short frames are tolerated and
    handled by consumers rather than rejected.
    """

    pcm: bytes

    @property
    def duration_ms(self) -> float:
        denom = bytes_per_ms(CALL_RATE, CALL_CHANNELS)
        return len(self.pcm) / denom if denom else 0.0

    def as_chunk(self) -> AudioChunk:
        return AudioChunk(pcm=self.pcm, rate=CALL_RATE, channels=CALL_CHANNELS)
