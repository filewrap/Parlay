"""Tests for FallbackVoiceProvider transport selection."""

from __future__ import annotations

import pytest

from parlay.voice.fallback import FallbackVoiceProvider
from parlay.voice.provider import ProviderError, ReplyEvent


class FakeProvider:
    def __init__(self, *, open_fails: bool = False) -> None:
        self.open_fails = open_fails
        self.opened = False
        self.closed = False
        self.sent: bytes | None = None

    @property
    def input_rate(self) -> int:
        return 16000

    @property
    def output_rate(self) -> int:
        return 24000

    async def open(self) -> None:
        if self.open_fails:
            raise ProviderError("boom")
        self.opened = True

    async def send_audio(self, pcm: bytes) -> None:
        self.sent = pcm

    async def events(self):
        yield ReplyEvent.turn_complete()

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_prefers_primary_when_it_opens():
    primary, secondary = FakeProvider(), FakeProvider()
    fb = FallbackVoiceProvider(primary, secondary)
    await fb.open()
    assert primary.opened and not secondary.opened


@pytest.mark.asyncio
async def test_falls_back_when_primary_open_fails():
    primary, secondary = FakeProvider(open_fails=True), FakeProvider()
    fb = FallbackVoiceProvider(primary, secondary)
    await fb.open()
    assert secondary.opened


@pytest.mark.asyncio
async def test_skips_missing_leg():
    secondary = FakeProvider()
    fb = FallbackVoiceProvider(None, secondary)
    await fb.open()
    assert secondary.opened


@pytest.mark.asyncio
async def test_raises_when_all_fail():
    fb = FallbackVoiceProvider(FakeProvider(open_fails=True), FakeProvider(open_fails=True))
    with pytest.raises(ProviderError):
        await fb.open()


def test_requires_at_least_one_leg():
    with pytest.raises(ProviderError):
        FallbackVoiceProvider(None, None)
