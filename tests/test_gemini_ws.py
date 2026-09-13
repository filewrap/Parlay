"""Tests for the raw-WebSocket Gemini Live transport."""

from __future__ import annotations

import base64
import json

import pytest

import parlay.voice.gemini_ws as gw
from parlay.voice.gemini_ws import GeminiLiveSocket
from parlay.voice.provider import ReplyEventKind, SessionConfiguration


class FakeTokens:
    def __init__(self) -> None:
        self.calls = 0

    async def token(self, *, force: bool = False) -> str:
        self.calls += 1
        return "tok"

    def invalidate(self) -> None:
        pass


class FakeWS:
    def __init__(self, incoming: list[str]) -> None:
        self.sent: list[str] = []
        self._incoming = list(incoming)
        self.closed = False

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def recv(self) -> str:
        return self._incoming.pop(0)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self) -> FakeWS:
        return self

    async def __anext__(self) -> str:
        if not self._incoming:
            raise StopAsyncIteration
        return self._incoming.pop(0)


@pytest.mark.asyncio
async def test_setup_send_and_reply_roundtrip(monkeypatch):
    audio = base64.b64encode(b"pcmpcm").decode()
    frames = [
        json.dumps({"setupComplete": {}}),
        json.dumps({"serverContent": {"modelTurn": {"parts": [{"inlineData": {"data": audio}}]}}}),
        json.dumps({"serverContent": {"turnComplete": True}}),
    ]
    fake = FakeWS(frames)

    async def fake_connect(url, **kwargs):
        assert "access_token=tok" in url
        return fake

    monkeypatch.setattr(gw, "connect", fake_connect)
    cfg = SessionConfiguration(model="gemini-x", voice="Puck", system_instruction="be nice")
    prov = GeminiLiveSocket(FakeTokens(), cfg)
    await prov.open()

    setup = json.loads(fake.sent[0])
    assert setup["setup"]["model"] == "models/gemini-x"
    assert setup["setup"]["generationConfig"]["responseModalities"] == ["AUDIO"]
    assert setup["setup"]["systemInstruction"]["parts"][0]["text"] == "be nice"

    await prov.send_audio(b"in")
    frame = json.loads(fake.sent[1])
    assert frame["realtimeInput"]["mediaChunks"][0]["mimeType"].startswith("audio/pcm")

    events = []
    async for event in prov.events():
        events.append(event)
        if event.kind is ReplyEventKind.TURN_COMPLETE:
            break
    kinds = [e.kind for e in events]
    assert ReplyEventKind.AUDIO in kinds
    assert ReplyEventKind.TURN_COMPLETE in kinds
    audio_event = next(e for e in events if e.kind is ReplyEventKind.AUDIO)
    assert audio_event.pcm == b"pcmpcm"


@pytest.mark.asyncio
async def test_open_failure_becomes_provider_error(monkeypatch):
    async def boom(url, **kwargs):
        raise OSError("no route")

    monkeypatch.setattr(gw, "connect", boom)
    prov = GeminiLiveSocket(FakeTokens(), SessionConfiguration(model="m"))
    with pytest.raises(gw.ProviderError):
        await prov.open()
