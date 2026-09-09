"""Raw audio bridge core.

Shared audio plumbing composed by every Parlay feature: it owns the two
ntgcalls raw-call callbacks, resamples between the 48 kHz call boundary and
consumer rates, and moves audio across the native-thread / asyncio boundary
through bounded queues.

The tgcalls-specific surface is isolated in `rawcall.py`; everything else in
this package is pure Python and unit-tested.
"""

from .frames import CALL_CHANNELS, CALL_RATE, FRAME_MS, AudioChunk, Pcm48kFrame

__all__ = [
    "CALL_CHANNELS",
    "CALL_RATE",
    "FRAME_MS",
    "AudioChunk",
    "Pcm48kFrame",
]
