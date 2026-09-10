"""Application command regression tests with native audio replaced at its boundary."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telethon.tl import types
from telethon.utils import get_peer_id

from parlay.app import ParlayApp
from parlay.commands import ParsedCommand
from parlay.vc import VcStatus, VoiceChatError


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr("parlay.app.build_client", lambda _: Mock())
    config = SimpleNamespace(operator_id="7", command_prefix="/", pot_provider_url="http://test")
    instance = ParlayApp(config)
    instance.vc = Mock()
    instance.vc.status = AsyncMock(return_value=VcStatus(True))
    instance.activity = Mock()
    instance._activity_ready = True
    instance.activity.reconcile = AsyncMock()
    instance.activity.set_transport = AsyncMock()
    bridge = Mock(start=AsyncMock(), stop=AsyncMock())
    monkeypatch.setattr("parlay.app.RawAudioBridge", lambda *a, **kw: bridge)
    monkeypatch.setattr("parlay.app.AudioOutputArbiter", lambda _: Mock())
    instance._build_music = Mock(return_value=Mock(on_session_end=AsyncMock()))
    return instance


def channel(broadcast=False):
    return types.Channel(
        id=202,
        title="Music",
        photo=types.ChatPhotoEmpty(),
        date=None,
        broadcast=broadcast,
        megagroup=not broadcast,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("broadcast", [False, True])
async def test_join_current_channel_and_repeat(app, broadcast):
    entity = channel(broadcast)
    chat_id = get_peer_id(entity)
    app.vc.resolve = AsyncMock(return_value=entity)
    command = ParsedCommand("join", "", "7", chat_id, not broadcast, True)
    assert "Connected Parlay" in await app._cmd_join(command)
    app.vc.resolve.assert_awaited_with(chat_id)
    app.activity.set_transport.assert_awaited_with(chat_id, True)
    assert "already connected" in await app._cmd_join(command)
    assert app._build_music.call_count == 1


@pytest.mark.asyncio
async def test_join_current_basic_group(app):
    entity = types.Chat(
        id=101,
        title="Group",
        photo=types.ChatPhotoEmpty(),
        participants_count=2,
        date=None,
        version=1,
    )
    app.vc.resolve = AsyncMock(return_value=entity)
    assert "Connected Parlay" in await app._cmd_join(ParsedCommand("join", "", "7", -101, True))
    app.vc.resolve.assert_awaited_with(-101)


@pytest.mark.asyncio
async def test_join_private_requires_target_and_explicit_overrides(app):
    assert "private messages" in await app._cmd_join(ParsedCommand("join", "", "7", 7))
    app.vc.resolve = AsyncMock(return_value=channel())
    await app._cmd_join(ParsedCommand("join", "@music", "7", -101, True))
    app.vc.resolve.assert_awaited_with("@music")


@pytest.mark.asyncio
async def test_non_group_target_and_missing_call(app):
    app.vc.resolve = AsyncMock(return_value=types.User(id=7))
    app.vc.status.side_effect = VoiceChatError("Voice chats exist only in groups and channels.")
    assert "only in groups" in await app._cmd_join(ParsedCommand("join", "7", "7"))
    assert not app.sessions.active
    app.vc.status.side_effect = None
    app.vc.status.return_value = VcStatus(False)
    assert "No active voice chat" in await app._cmd_join(ParsedCommand("join", "@music", "7"))


@pytest.mark.asyncio
async def test_failed_join_rolls_back(app, monkeypatch):
    app.vc.resolve = AsyncMock(return_value=channel())
    bridge = Mock(start=AsyncMock(side_effect=RuntimeError("failed")), stop=AsyncMock())
    monkeypatch.setattr("parlay.app.RawAudioBridge", lambda *a, **kw: bridge)
    assert "Could not connect" in await app._cmd_join(ParsedCommand("join", "@music", "7"))
    assert not app.sessions.active
    assert app._media_chat_id is None
    bridge.stop.assert_awaited()


@pytest.mark.asyncio
async def test_activity_cleanup_is_chat_scoped_and_duplicate_safe(app):
    app.vc.resolve = AsyncMock(return_value=channel())
    await app._cmd_join(ParsedCommand("join", "@music", "7"))
    chat_id = app._media_chat_id
    app._notify_operator = AsyncMock()
    await app._on_activity_unavailable(-999, "call_discarded")
    assert app.sessions.active
    await app._on_activity_unavailable(chat_id, "call_discarded")
    await app._on_activity_unavailable(chat_id, "call_discarded")
    assert not app.sessions.active
    app._notify_operator.assert_awaited_once()
    app.activity.set_transport.assert_awaited_with(chat_id, False)


@pytest.mark.asyncio
async def test_raw_callback_returns_while_media_lock_held(app):
    app.vc.resolve = AsyncMock(return_value=channel())
    await app._cmd_join(ParsedCommand("join", "@music", "7"))
    app._notify_operator = AsyncMock()
    async with app._media_lock:
        await asyncio.wait_for(
            app._queue_activity_unavailable(app._media_chat_id, "call_discarded"), 0.2
        )
        await asyncio.sleep(0)
        assert app.sessions.active
    await asyncio.gather(*tuple(app._activity_jobs))
    assert not app.sessions.active


@pytest.mark.asyncio
async def test_old_queued_callback_cannot_close_rejoined_session(app):
    app.vc.resolve = AsyncMock(return_value=channel())
    await app._cmd_join(ParsedCommand("join", "@music", "7"))
    chat_id = app._media_chat_id
    async with app._media_lock:
        await app._queue_activity_unavailable(chat_id, "call_discarded")
        await asyncio.sleep(0)
        app.sessions.end()
        app.sessions.begin_join(str(chat_id))
        app.sessions.mark_connected()
    await asyncio.gather(*tuple(app._activity_jobs))
    assert app.sessions.active


@pytest.mark.asyncio
async def test_cancelled_join_rolls_back(app, monkeypatch):
    app.vc.resolve = AsyncMock(return_value=channel())
    bridge = Mock(start=AsyncMock(side_effect=asyncio.CancelledError()), stop=AsyncMock())
    monkeypatch.setattr("parlay.app.RawAudioBridge", lambda *a, **kw: bridge)
    with pytest.raises(asyncio.CancelledError):
        await app._cmd_join(ParsedCommand("join", "@music", "7"))
    assert not app.sessions.active
    assert app._media_chat_id is None
    bridge.stop.assert_awaited()
