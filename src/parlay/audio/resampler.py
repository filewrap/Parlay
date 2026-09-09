"""Audio resampling and channel conversion.

Centralizes every sample-rate and channel conversion on the bridge so
consumers stay format-agnostic (blueprint ADR-002). Uses numpy linear
interpolation, which is cheap and good enough for speech at these rates.

Conversions needed:
  * 48 kHz stereo call audio -> 16 kHz mono for Gemini input
  * 24 kHz mono Gemini output -> 48 kHz stereo for playback
  * general rate/channel changes for other consumers

All PCM is S16LE.
"""

from __future__ import annotations

import numpy as np

from .frames import CALL_CHANNELS, CALL_RATE, AudioChunk, Pcm48kFrame


def _decode(pcm: bytes) -> np.ndarray:
    """Bytes S16LE -> float32 array in [-1, 1]. Odd trailing byte is dropped."""
    if len(pcm) % 2:
        pcm = pcm[:-1]
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    return samples / 32768.0


def _encode(samples: np.ndarray) -> bytes:
    """float32 array in [-1, 1] -> bytes S16LE, clipped."""
    clipped = np.clip(samples, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


def _to_channels(mono_or_multi: np.ndarray, src_ch: int, dst_ch: int) -> np.ndarray:
    """Convert interleaved samples between channel counts.

    Works on a flat interleaved array; returns a flat interleaved array.
    """
    if src_ch == dst_ch:
        return mono_or_multi
    frames = mono_or_multi.reshape(-1, src_ch)
    if dst_ch == 1:
        # Downmix by averaging channels.
        return frames.mean(axis=1)
    if src_ch == 1:
        # Upmix mono to N channels by duplication.
        return np.repeat(frames, dst_ch, axis=1).reshape(-1)
    # General case: take the first min(src,dst) channels, pad by repeat.
    mono = frames.mean(axis=1)
    return np.repeat(mono.reshape(-1, 1), dst_ch, axis=1).reshape(-1)


def _resample_rate(mono: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Linear-interpolate a mono signal from src_rate to dst_rate."""
    if src_rate == dst_rate or mono.size == 0:
        return mono
    duration = mono.size / src_rate
    dst_n = int(round(duration * dst_rate))
    if dst_n <= 0:
        return np.zeros(0, dtype=np.float32)
    # Sample positions in the source timeline.
    src_idx = np.linspace(0.0, mono.size - 1, num=dst_n, dtype=np.float32)
    src_points = np.arange(mono.size, dtype=np.float32)
    return np.interp(src_idx, src_points, mono).astype(np.float32)


class AudioResampler:
    """Stateless converter between rate/channel formats."""

    def convert(
        self,
        chunk: AudioChunk,
        dst_rate: int,
        dst_channels: int,
    ) -> AudioChunk:
        """Convert an AudioChunk to the requested rate and channel count."""
        if chunk.rate == dst_rate and chunk.channels == dst_channels:
            return chunk
        samples = _decode(chunk.pcm)
        if samples.size == 0:
            return AudioChunk(pcm=b"", rate=dst_rate, channels=dst_channels)
        # Convert to mono first for a single-rate interpolation, then re-expand.
        mono = _to_channels(samples, chunk.channels, 1)
        mono = _resample_rate(mono, chunk.rate, dst_rate)
        out = _to_channels(mono, 1, dst_channels)
        return AudioChunk(pcm=_encode(out), rate=dst_rate, channels=dst_channels)

    def capture_to(self, frame: Pcm48kFrame, dst_rate: int, dst_channels: int) -> AudioChunk:
        """Down-convert a 48 kHz stereo call frame for a consumer."""
        return self.convert(frame.as_chunk(), dst_rate, dst_channels)

    def to_call(self, chunk: AudioChunk) -> Pcm48kFrame:
        """Up-convert provider audio to a 48 kHz stereo call frame."""
        converted = self.convert(chunk, CALL_RATE, CALL_CHANNELS)
        return Pcm48kFrame(pcm=converted.pcm)
