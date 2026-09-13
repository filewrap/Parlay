"""Tests for the AI Voice Pipeline: ProviderSessionManager and reply events.

Uses fakes for the VoiceProvider and the bridge, so no network or native SDK is
required.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

from parlay.audio.frames import AudioChunk
from parlay.voice.gemini import DEFAULT_MODEL, default_configuration
from parlay.voice.provider import (
    ProviderError,
    ReplyEvent,
    ReplyEventKind,
    ResponseModality,
    SessionConfiguration,
    VoiceProvider,
)
from parlay.voice.session_manager import ProviderSessionManager


class FakeProvider:
    """A scripted VoiceProvider that emits a fixed list of reply events."""

    def __init__(self, events: list[ReplyEvent], *, open_error: bool = False) -> None:
        self._events = events
        self._open_error = open_error
        self.sent: list[bytes] = []
        self.opened = False
        self.closed = False
        self._release = asyncio.Event()

    @property
    def input_rate(self) -> int:
        return 16_000

    @property
    def output_rate(self) -> int:
        return 24_000

    async def open(self) -> None:
        if self._open_error:
            raise ProviderError("boom")
        self.opened = True

    async def send_audio(self, pcm: bytes) -> None:
        self.sent.append(pcm)

    async def events(self) -> AsyncIterator[ReplyEvent]:
        for event in self._events:
            yield event
        # Keep the stream open so the manager does not treat exhaustion as a
        # loss during the test; the test cancels it via disengage().
        await self._release.wait()

    async def close(self) -> None:
        self.closed = True
        self._release.set()


class FakeBridge:
    """Captured-source + playback-sink double recording all interactions."""

    def __init__(self) -> None:
        self.consumer: Callable[[AudioChunk], Awaitable[None]] | None = None
        self.sub_rate: int | None = None
        self.sub_channels: int | None = None
        self.token_released = False
        self.played: list[AudioChunk] = []
        self.interrupts = 0

    def subscribe(
        self,
        callback: Callable[[AudioChunk], Awaitable[None]],
        rate: int,
        channels: int,
    ) -> int:
        self.consumer = callback
        self.sub_rate = rate
        self.sub_channels = channels
        return 7

    def unsubscribe(self, token: int) -> None:
        self.token_released = True

    def play_chunk(self, chunk: AudioChunk) -> None:
        self.played.append(chunk)

    def interrupt(self) -> None:
        self.interrupts += 1


def _reply_pcm(bridge: FakeBridge) -> bytes:
    """Concatenate played PCM and drop the leading silence gap.

    Each turn is led by a gap of S16LE silence; the model audio in these tests
    is non-zero, so stripping leading NUL bytes leaves just the reply audio.
    """
    joined = b"".join(chunk.pcm for chunk in bridge.played)
    return joined.lstrip(b"\x00")


def test_fake_provider_satisfies_interface() -> None:
    assert isinstance(FakeProvider([]), VoiceProvider)


async def test_engage_opens_and_subscribes_at_provider_rate_mono() -> None:
    provider = FakeProvider([])
    bridge = FakeBridge()
    mgr = ProviderSessionManager(provider, bridge, bridge)
    await mgr.engage()
    try:
        assert provider.opened
        assert mgr.engaged
        assert bridge.sub_rate == 16_000
        assert bridge.sub_channels == 1
    finally:
        await mgr.disengage()


async def test_engage_failure_leaves_pipeline_disengaged() -> None:
    provider = FakeProvider([], open_error=True)
    bridge = FakeBridge()
    mgr = ProviderSessionManager(provider, bridge, bridge)
    try:
        await mgr.engage()
        raise AssertionError("expected ProviderError")
    except ProviderError:
        pass
    assert not mgr.engaged
    assert bridge.consumer is None  # never subscribed


async def test_captured_audio_is_forwarded_to_provider() -> None:
    provider = FakeProvider([])
    bridge = FakeBridge()
    mgr = ProviderSessionManager(provider, bridge, bridge)
    await mgr.engage()
    try:
        assert bridge.consumer is not None
        await bridge.consumer(AudioChunk(pcm=b"\x01\x00" * 160, rate=16_000, channels=1))
        assert provider.sent == [b"\x01\x00" * 160]
    finally:
        await mgr.disengage()


async def test_reply_audio_is_played_at_output_rate_mono() -> None:
    # The turn's audio is held as pre-roll and released when the turn completes.
    audio = b"\x02\x00" * 240
    provider = FakeProvider([ReplyEvent.audio(audio), ReplyEvent.turn_complete()])
    bridge = FakeBridge()
    mgr = ProviderSessionManager(provider, bridge, bridge)
    await mgr.engage()
    try:
        await asyncio.sleep(0.02)  # let the drain task run
        assert bridge.played  # gap + released audio
        for chunk in bridge.played:
            assert chunk.rate == 24_000
            assert chunk.channels == 1
        assert _reply_pcm(bridge) == audio
    finally:
        await mgr.disengage()


async def test_turn_leads_with_a_silence_gap() -> None:
    audio = b"\x02\x00" * 240
    provider = FakeProvider([ReplyEvent.audio(audio), ReplyEvent.turn_complete()])
    bridge = FakeBridge()
    mgr = ProviderSessionManager(provider, bridge, bridge)
    await mgr.engage()
    try:
        await asyncio.sleep(0.02)
        joined = b"".join(chunk.pcm for chunk in bridge.played)
        # A leading run of silence precedes the model audio.
        assert joined.startswith(b"\x00\x00")
        assert len(joined) > len(audio)
    finally:
        await mgr.disengage()


async def test_interruption_flushes_playback() -> None:
    provider = FakeProvider([ReplyEvent.audio(b"\x02\x00" * 10), ReplyEvent.interrupted()])
    bridge = FakeBridge()
    mgr = ProviderSessionManager(provider, bridge, bridge)
    await mgr.engage()
    try:
        await asyncio.sleep(0.02)
        assert bridge.interrupts >= 1
    finally:
        await mgr.disengage()


async def test_turn_complete_does_not_flush() -> None:
    provider = FakeProvider([ReplyEvent.audio(b"\x02\x00" * 10), ReplyEvent.turn_complete()])
    bridge = FakeBridge()
    mgr = ProviderSessionManager(provider, bridge, bridge)
    await mgr.engage()
    try:
        await asyncio.sleep(0.02)
        assert bridge.played  # audio released on turn complete
        # disengage() flushes once on teardown; no flush from turn_complete itself
        assert bridge.interrupts == 0
    finally:
        await mgr.disengage()


async def test_reply_boundary_hooks_fire_once_per_turn() -> None:
    provider = FakeProvider(
        [
            ReplyEvent.audio(b"\x02\x00" * 8),
            ReplyEvent.audio(b"\x02\x00" * 8),
            ReplyEvent.turn_complete(),
        ]
    )
    bridge = FakeBridge()
    starts = 0
    ends = 0

    async def on_start() -> None:
        nonlocal starts
        starts += 1

    async def on_end() -> None:
        nonlocal ends
        ends += 1

    mgr = ProviderSessionManager(
        provider, bridge, bridge, on_reply_start=on_start, on_reply_end=on_end
    )
    await mgr.engage()
    try:
        await asyncio.sleep(0.02)
        assert starts == 1
        assert ends == 1
    finally:
        await mgr.disengage()


async def test_note_speaker_forwards_context_when_supported() -> None:
    contexts: list[str] = []

    class ContextProvider(FakeProvider):
        async def send_context(self, text: str) -> None:
            contexts.append(text)

    provider = ContextProvider([])
    bridge = FakeBridge()
    mgr = ProviderSessionManager(provider, bridge, bridge)
    await mgr.engage()
    try:
        mgr.note_speaker("Asha")
        mgr.note_speaker("Asha")  # same speaker: no duplicate context
        await asyncio.sleep(0.02)
        assert len(contexts) == 1
        assert "Asha" in contexts[0]
    finally:
        await mgr.disengage()


async def test_disengage_detaches_and_closes() -> None:
    provider = FakeProvider([])
    bridge = FakeBridge()
    mgr = ProviderSessionManager(provider, bridge, bridge)
    await mgr.engage()
    await mgr.disengage()
    assert not mgr.engaged
    assert bridge.token_released
    assert provider.closed
    assert bridge.interrupts == 1  # teardown flush


async def test_unrecoverable_loss_reports_and_disengages() -> None:
    class LosingProvider(FakeProvider):
        async def events(self) -> AsyncIterator[ReplyEvent]:
            raise ProviderError("session gone")
            yield  # pragma: no cover - makes this an async generator

    provider = LosingProvider([])
    bridge = FakeBridge()
    reasons: list[str] = []

    async def on_loss(reason: str) -> None:
        reasons.append(reason)

    mgr = ProviderSessionManager(provider, bridge, bridge, on_loss=on_loss)
    await mgr.engage()
    await asyncio.sleep(0.02)
    assert not mgr.engaged
    assert reasons  # Operator was notified
    assert bridge.token_released


async def test_first_reply_audio_announces_speaking_once_per_turn() -> None:
    provider = FakeProvider(
        [
            ReplyEvent.audio(b"\x02\x00" * 8),
            ReplyEvent.audio(b"\x02\x00" * 8),
            ReplyEvent.turn_complete(),
        ]
    )
    bridge = FakeBridge()
    announces = 0

    async def on_speaking() -> None:
        nonlocal announces
        announces += 1

    mgr = ProviderSessionManager(provider, bridge, bridge, on_speaking=on_speaking)
    await mgr.engage()
    try:
        await asyncio.sleep(0.02)
        # Two audio chunks in one turn announce exactly once (AC-AIVP-008.1).
        assert announces == 1
    finally:
        await mgr.disengage()


async def test_speaking_announcement_is_throttled_across_turns() -> None:
    provider = FakeProvider(
        [
            ReplyEvent.audio(b"\x02\x00" * 8),
            ReplyEvent.turn_complete(),
            ReplyEvent.audio(b"\x02\x00" * 8),
            ReplyEvent.turn_complete(),
        ]
    )
    bridge = FakeBridge()
    announces = 0

    async def on_speaking() -> None:
        nonlocal announces
        announces += 1

    mgr = ProviderSessionManager(provider, bridge, bridge, on_speaking=on_speaking)
    await mgr.engage()
    try:
        await asyncio.sleep(0.02)
        # Two separate turns inside the throttle window announce at most once
        # so the chat is not flooded (AC-AIVP-008.3).
        assert announces == 1
    finally:
        await mgr.disengage()


def test_default_configuration_requests_native_audio() -> None:
    config = default_configuration()
    assert config.model == DEFAULT_MODEL
    assert config.response_modality is ResponseModality.AUDIO


def test_session_configuration_defaults_to_audio_modality() -> None:
    config = SessionConfiguration(model="some-model")
    assert config.response_modality is ResponseModality.AUDIO
    assert config.voice is None
    assert config.system_instruction is None


def test_reply_event_kinds() -> None:
    assert ReplyEvent.audio(b"x").kind is ReplyEventKind.AUDIO
    assert ReplyEvent.turn_complete().kind is ReplyEventKind.TURN_COMPLETE
    assert ReplyEvent.interrupted().kind is ReplyEventKind.INTERRUPTED
