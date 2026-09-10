"""Application boundary regression tests for concurrent room and VC orchestration."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telethon.tl import types
from telethon.utils import get_peer_id

from parlay.app import ParlayApp
from parlay.commands import ParsedCommand
from parlay.vc import VcStatus


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr("parlay.app.build_client", lambda _: Mock())
    config = SimpleNamespace(operator_id="7", command_prefix="/", pot_provider_url="http://test")
    instance = ParlayApp(config)
    instance.vc = Mock()
    instance.vc.status = AsyncMock(return_value=VcStatus(True))
    instance.vc.get_active_call = AsyncMock(return_value=types.InputGroupCall(999, 123))
    instance.activity = Mock(reconcile=AsyncMock(), set_transport=AsyncMock())
    instance._activity_ready = True
    runtimes = {}
    instance.registry = Mock()
    instance.registry.get = lambda chat_id: runtimes.get(chat_id)
    async def join(chat_id):
        if chat_id not in runtimes:
            runtimes[chat_id] = Mock(call_id=None, generation=1)
        return runtimes[chat_id]
    async def leave(chat_id, reason="left"):
        runtimes.pop(chat_id, None)
    instance.registry.join = AsyncMock(side_effect=join)
    instance.registry.leave = AsyncMock(side_effect=leave)
    instance.registry.command = AsyncMock(return_value={"track": None, "queue": []})
    return instance


def channel(broadcast=False):
    return types.Channel(id=202, title="Music", photo=types.ChatPhotoEmpty(), date=None, broadcast=broadcast, megagroup=not broadcast)


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


@pytest.mark.asyncio
async def test_join_current_basic_group(app):
    entity = types.Chat(id=101, title="Group", photo=types.ChatPhotoEmpty(), participants_count=2, date=None, version=1)
    app.vc.resolve = AsyncMock(return_value=entity)
    assert "Connected Parlay" in await app._cmd_join(ParsedCommand("join", "", "7", -101, True))
    app.vc.resolve.assert_awaited_with(-101)


@pytest.mark.asyncio
async def test_private_requires_explicit_target(app):
    assert "specify a chat" in await app._cmd_join(ParsedCommand("join", "", "7", 7))
    app.vc.resolve = AsyncMock(return_value=channel())
    await app._cmd_join(ParsedCommand("join", "@music", "7", -101, True))
    app.vc.resolve.assert_awaited_with("@music")


@pytest.mark.asyncio
async def test_non_group_and_missing_call(app):
    app.vc.resolve = AsyncMock(return_value=types.User(id=7))
    assert "only in groups" in await app._cmd_join(ParsedCommand("join", "7", "7"))
    app.registry.join.assert_not_awaited()
    app.vc.resolve.return_value = channel()
    app.vc.get_active_call.return_value = None
    assert "No active voice chat" in await app._cmd_join(ParsedCommand("join", "@music", "7"))


@pytest.mark.asyncio
async def test_failed_or_cancelled_join_does_not_mark_transport(app):
    app.registry.join.side_effect = RuntimeError("native join failed")
    with pytest.raises(RuntimeError):
        await app._join_chat(-101)
    app.activity.set_transport.assert_not_awaited()
    app.registry.join.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await app._join_chat(-101)
    app.activity.set_transport.assert_not_awaited()


@pytest.mark.asyncio
async def test_two_concurrent_calls_leave_one_only(app):
    await asyncio.gather(app._join_chat(-101), app._join_chat(-202))
    assert app.registry.get(-101) is not None
    assert app.registry.get(-202) is not None
    await app._queue_activity_unavailable(-101, "call_discarded")
    await asyncio.gather(*tuple(app._jobs))
    assert app.registry.get(-101) is None
    assert app.registry.get(-202) is not None


@pytest.mark.asyncio
async def test_stale_activity_callback_does_not_close_replacement(app):
    await app._join_chat(-101)
    await app._queue_activity_unavailable(-101, "call_discarded")
    await app.registry.leave(-101)
    await app.registry.join(-101)
    await asyncio.gather(*tuple(app._jobs))
    assert app.registry.get(-101) is not None


@pytest.mark.asyncio
async def test_room_controls_use_same_registry_without_recursive_publish(app):
    await app._join_chat(-101)
    app.rooms = Mock(publish_playback=AsyncMock())
    track = {"id": "abcdefghijk", "source_url": "https://www.youtube.com/watch?v=abcdefghijk"}
    await app._room_playback(-101, "queue_add", {"track": track})
    app.registry.command.assert_awaited_with(-101, "play", track["source_url"])
    app.rooms.publish_playback.assert_not_awaited()


@pytest.mark.asyncio
async def test_authority_owner_not_any_admin(app):
    app._participant = AsyncMock(return_value=types.ChatParticipant(user_id=9, inviter_id=7, date=None))
    assert not await app._authority(9, -101)
    app._participant.return_value = types.ChatParticipantCreator(user_id=9)
    assert await app._authority(9, -101)
    assert await app._authority(7, -101)


@pytest.mark.asyncio
async def test_play_event_attributed_to_requester_not_other_members(app):
    app._authority = AsyncMock(return_value=True)
    track = {"id": "abcdefghijk", "title": "A song", "source_url": "https://www.youtube.com/watch?v=abcdefghijk"}
    app.search = AsyncMock(return_value=[track])
    app.compass = Mock()
    await app._play_for_user(7, -101, "song")
    assert app.compass.record_event.call_args.args[:3] == ("7", "abcdefghijk", "play")
    assert app.compass.record_event.call_count == 1
