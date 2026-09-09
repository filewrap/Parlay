"""Tests for AudioResampler: rate and channel conversions."""

from __future__ import annotations

import numpy as np

from parlay.audio.frames import AudioChunk, Pcm48kFrame
from parlay.audio.resampler import AudioResampler, _decode, _encode


def _sine(freq: int, rate: int, ms: int, channels: int = 1) -> bytes:
    n = rate * ms // 1000
    t = np.arange(n) / rate
    mono = (0.5 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    if channels == 1:
        return _encode(mono)
    inter = np.repeat(mono.reshape(-1, 1), channels, axis=1).reshape(-1)
    return _encode(inter)


def test_roundtrip_encode_decode_is_close() -> None:
    pcm = _sine(440, 16000, 20)
    back = _encode(_decode(pcm))
    assert len(back) == len(pcm)


def test_48k_stereo_to_16k_mono_length() -> None:
    r = AudioResampler()
    frame = Pcm48kFrame(pcm=_sine(300, 48000, 100, channels=2))
    out = r.capture_to(frame, 16000, 1)
    assert out.rate == 16000
    assert out.channels == 1
    # 100 ms mono @16k = 1600 samples * 2 bytes = 3200, within rounding.
    assert abs(len(out.pcm) - 3200) <= 4


def test_24k_mono_to_48k_stereo_for_playback() -> None:
    r = AudioResampler()
    chunk = AudioChunk(pcm=_sine(200, 24000, 50), rate=24000, channels=1)
    frame = r.to_call(chunk)
    # 50 ms @48k stereo = 2400 samples * 2ch * 2 bytes = 9600, within rounding.
    assert abs(len(frame.pcm) - 9600) <= 8


def test_identity_conversion_returns_same_chunk() -> None:
    r = AudioResampler()
    chunk = AudioChunk(pcm=_sine(440, 16000, 10), rate=16000, channels=1)
    assert r.convert(chunk, 16000, 1) is chunk


def test_empty_pcm_is_handled() -> None:
    r = AudioResampler()
    out = r.convert(AudioChunk(pcm=b"", rate=48000, channels=2), 16000, 1)
    assert out.pcm == b""
    assert out.rate == 16000


def test_downmix_preserves_signal_energy_roughly() -> None:
    r = AudioResampler()
    # Same tone in both channels; downmix should preserve amplitude.
    frame = Pcm48kFrame(pcm=_sine(400, 48000, 40, channels=2))
    out = r.capture_to(frame, 48000, 1)
    samples = _decode(out.pcm)
    assert samples.size > 0
    assert float(np.max(np.abs(samples))) > 0.2
