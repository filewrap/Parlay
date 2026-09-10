"""Tests for the py-tgcalls (NTgCalls) raw-call adapter.

The real binding is replaced with fakes shaped exactly like the verified
py-tgcalls 2.3.x surface: PyTgCalls(client), start, resolve_chat_id, play,
record, send_frame, add_handler/remove_handler, leave_call, and the
StreamFrames / ChatUpdate update types with Flag semantics.
"""

from __future__ import annotations

import asyncio
from enum import Flag, auto
from types import SimpleNamespace

import pytest

from parlay.audio import rawcall
from parlay.audio.rawcall import FRAME_BYTES, RawCallAdapter


class Direction(Flag):
    OUTGOING = auto()
    INCOMING = auto()


class Device(Flag):
    MICROPHONE = auto()
    SPEAKER = auto()


class ExternalMedia(Flag):
    AUDIO = auto()
    VIDEO = auto()


class AudioQuality:
    HIGH = (48000, 2)


class MediaStream:
    def __init__(self, media_path, audio_parameters=None):
        self.media_path = media_path
        self.audio_parameters = audio_parameters


class RecordStream:
    def __init__(self, audio=False, audio_parameters=None):
        self.audio = audio
        self.audio_parameters = audio_parameters


class Update:
    def __init__(self, chat_id):
        self.chat_id = chat_id


class FakeFrame:
    def __init__(self, data: bytes):
        self.ssrc = 1
        self.frame = data


class StreamFrames(Update):
    def __init__(self, chat_id, direction, device, frames):
        super().__init__(chat_id)
        self.direction = direction
        self.device = device
        self.frames = frames


class ChatUpdate(Update):
    class Status(Flag):
        KICKED = auto()
        LEFT_GROUP = auto()
        CLOSED_VOICE_CHAT = auto()
        DISCARDED_CALL = auto()
        BUSY_CALL = auto()
        INVITED_VOICE_CHAT = auto()
        LEFT_CALL = KICKED | LEFT_GROUP | CLOSED_VOICE_CHAT | DISCARDED_CALL | BUSY_CALL

    def __init__(self, chat_id, status):
        super().__init__(chat_id)
        self.status = status


class FakePyTgCalls:
    def __init__(self, client):
        self.client = client
        self.started = False
        self.handlers: list = []
        self.play_calls: list = []
        self.record_calls: list = []
        self.sent_frames: list = []
        self.left: list = []

    async def start(self):
        self.started = True

    async def resolve_chat_id(self, chat):
        return -100123

    def add_handler(self, func, filters=None):
        self.handlers.append(func)
        return func

    def remove_handler(self, func):
        self.handlers = [h for h in self.handlers if h != func]

    async def play(self, chat_id, stream=None, config=None):
        self.play_calls.append((chat_id, stream))

    async def record(self, chat_id, stream=None, config=None):
        self.record_calls.append((chat_id, stream))

    async def send_frame(self, chat_id, device, data, frame_data=None):
        self.sent_frames.append((chat_id, device, data))

    async def leave_call(self, chat_id):
        self.left.append(chat_id)


def _fake_api() -> SimpleNamespace:
    return SimpleNamespace(
        PyTgCalls=FakePyTgCalls,
        AudioQuality=AudioQuality,
        ChatUpdate=ChatUpdate,
        Device=Device,
        Direction=Direction,
        ExternalMedia=ExternalMedia,
        MediaStream=MediaStream,
        RecordStream=RecordStream,
        StreamFrames=StreamFrames,
    )


@pytest.fixture
def adapter_env(monkeypatch):
    monkeypatch.setattr(rawcall, "_load_api", _fake_api)
    monkeypatch.setattr(rawcall, "_apps", {})
    monkeypatch.setattr(rawcall, "_started", set())
    recorded: list[tuple[bytes, int]] = []
    played: list[int] = []
    disconnects: list[bool] = []

    def on_recorded(data: bytes, length: int) -> None:
        recorded.append((data, length))

    def on_played(length: int) -> bytes:
        played.append(length)
        return b"\x01" * length

    adapter = RawCallAdapter(
        client=object(),
        on_recorded=on_recorded,
        on_played=on_played,
        on_disconnect=lambda: disconnects.append(True),
    )
    return SimpleNamespace(
        adapter=adapter,
        recorded=recorded,
        played=played,
        disconnects=disconnects,
    )


async def _start(env) -> FakePyTgCalls:
    await env.adapter.start("@somechat")
    (app,) = rawcall._apps.values()
    return app


async def test_start_joins_with_external_audio_and_records(adapter_env):
    app = await _start(adapter_env)
    assert app.started
    chat_id, stream = app.play_calls[0]
    assert chat_id == -100123
    assert stream.media_path == ExternalMedia.AUDIO
    chat_id, stream = app.record_calls[0]
    assert chat_id == -100123
    assert stream.audio is True
    assert app.handlers, "update handler must be registered"
    await adapter_env.adapter.stop()


async def test_pump_sends_paced_external_frames(adapter_env):
    app = await _start(adapter_env)
    await asyncio.sleep(0.05)
    assert app.sent_frames, "pump should have pushed frames"
    chat_id, device, data = app.sent_frames[0]
    assert chat_id == -100123
    assert device == Device.MICROPHONE
    assert len(data) == FRAME_BYTES
    assert adapter_env.played and adapter_env.played[0] == FRAME_BYTES
    await adapter_env.adapter.stop()


async def test_incoming_frames_forwarded_per_frame(adapter_env):
    app = await _start(adapter_env)
    handler = app.handlers[0]
    frames = [FakeFrame(b"aa"), FakeFrame(b"bbbb")]
    await handler(app, StreamFrames(-100123, Direction.INCOMING, Device.SPEAKER, frames))
    assert adapter_env.recorded == [(b"aa", 2), (b"bbbb", 4)]
    # Frames for another chat are ignored.
    await handler(app, StreamFrames(-999, Direction.INCOMING, Device.SPEAKER, frames))
    assert len(adapter_env.recorded) == 2
    # Outgoing echoes are ignored.
    await handler(app, StreamFrames(-100123, Direction.OUTGOING, Device.MICROPHONE, frames))
    assert len(adapter_env.recorded) == 2
    await adapter_env.adapter.stop()


async def test_left_call_status_triggers_disconnect(adapter_env):
    app = await _start(adapter_env)
    handler = app.handlers[0]
    await handler(app, ChatUpdate(-100123, ChatUpdate.Status.KICKED))
    assert adapter_env.disconnects == [True]
    # A non-terminal status does not fire it.
    await handler(app, ChatUpdate(-100123, ChatUpdate.Status.INVITED_VOICE_CHAT))
    assert adapter_env.disconnects == [True]
    await adapter_env.adapter.stop()


async def test_stop_leaves_call_and_unregisters(adapter_env):
    app = await _start(adapter_env)
    await adapter_env.adapter.stop()
    assert app.left == [-100123]
    assert app.handlers == []
    # Stop is idempotent.
    await adapter_env.adapter.stop()
    assert app.left == [-100123]


async def test_engine_reused_across_adapters(adapter_env):
    app = await _start(adapter_env)
    await adapter_env.adapter.stop()
    second = RawCallAdapter(
        client=adapter_env.adapter._client,
        on_recorded=lambda d, n: None,
        on_played=lambda n: b"",
        on_disconnect=None,
    )
    await second.start("@somechat")
    assert len(rawcall._apps) == 1, "one engine per client"
    await second.stop()
    assert app.left == [-100123, -100123]
