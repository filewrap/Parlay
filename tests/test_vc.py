"""Tests for the Telethon-native VoiceChatController.

Uses real Telethon TL types for entities (so the controller's isinstance
routing is exercised) and a fake client that answers raw requests by type.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from telethon.tl import types

from parlay.vc import VcStatus, VoiceChatController, VoiceChatError


def make_chat(default_banned_send: bool | None = None) -> types.Chat:
    banned = None
    if default_banned_send is not None:
        banned = types.ChatBannedRights(until_date=None, send_messages=default_banned_send)
    return types.Chat(
        id=101,
        title="basic group",
        photo=types.ChatPhotoEmpty(),
        participants_count=3,
        date=None,
        version=1,
        default_banned_rights=banned,
    )


def make_channel(
    broadcast: bool = False,
    default_banned_send: bool | None = None,
) -> types.Channel:
    banned = None
    if default_banned_send is not None:
        banned = types.ChatBannedRights(until_date=None, send_messages=default_banned_send)
    return types.Channel(
        id=202,
        title="channel",
        photo=types.ChatPhotoEmpty(),
        date=None,
        broadcast=broadcast,
        megagroup=not broadcast,
        default_banned_rights=banned,
    )


class FakeClient:
    """Answers Telethon calls; raw requests are routed by request class name."""

    def __init__(self, entity, call=None, group_call=None, participant=None):
        self.entity = entity
        self.call = call
        self.group_call = group_call
        self.participant = participant
        self.requests: list = []
        self.sent: list = []

    async def get_entity(self, ref):
        return self.entity

    async def get_input_entity(self, entity):
        return entity

    async def get_me(self, input_peer=False):
        return SimpleNamespace(user_id=7)

    async def send_message(self, entity, text):
        self.sent.append((entity, text))
        return "sent"

    async def __call__(self, request):
        self.requests.append(request)
        name = type(request).__name__
        if name in ("GetFullChannelRequest", "GetFullChatRequest"):
            return SimpleNamespace(full_chat=SimpleNamespace(call=self.call))
        if name == "GetGroupCallRequest":
            return SimpleNamespace(call=self.group_call)
        if name == "CreateGroupCallRequest":
            self.call = SimpleNamespace(id=1, access_hash=2)
            return SimpleNamespace()
        if name == "DiscardGroupCallRequest":
            self.call = None
            return SimpleNamespace()
        if name == "GetParticipantRequest":
            return SimpleNamespace(participant=self.participant)
        raise AssertionError(f"unexpected request {name}")


async def test_status_inactive_when_no_call():
    client = FakeClient(make_chat(), call=None)
    vc = VoiceChatController(client)
    assert await vc.status("g") == VcStatus(active=False)


async def test_status_reports_participants_and_title():
    info = SimpleNamespace(participants_count=4, title="hangout")
    client = FakeClient(make_chat(), call=SimpleNamespace(id=1), group_call=info)
    vc = VoiceChatController(client)
    status = await vc.status("g")
    assert status == VcStatus(active=True, participants=4, title="hangout")


async def test_status_discarded_call_is_inactive():
    discarded = types.GroupCallDiscarded(id=1, access_hash=2, duration=10)
    client = FakeClient(make_chat(), call=SimpleNamespace(id=1), group_call=discarded)
    vc = VoiceChatController(client)
    assert await vc.status("g") == VcStatus(active=False)


async def test_status_rejects_users():
    client = FakeClient(types.User(id=9), call=None)
    vc = VoiceChatController(client)
    with pytest.raises(VoiceChatError):
        await vc.status("someone")


async def test_start_creates_call_and_returns_it():
    client = FakeClient(make_chat(), call=None)
    vc = VoiceChatController(client)
    call = await vc.start("g")
    assert call is not None
    assert any(type(r).__name__ == "CreateGroupCallRequest" for r in client.requests)


async def test_start_refuses_when_already_active():
    client = FakeClient(make_chat(), call=SimpleNamespace(id=1))
    vc = VoiceChatController(client)
    with pytest.raises(VoiceChatError):
        await vc.start("g")


async def test_stop_discards_active_call():
    client = FakeClient(make_chat(), call=SimpleNamespace(id=1))
    vc = VoiceChatController(client)
    await vc.stop("g")
    assert any(type(r).__name__ == "DiscardGroupCallRequest" for r in client.requests)


async def test_stop_without_call_raises():
    client = FakeClient(make_chat(), call=None)
    vc = VoiceChatController(client)
    with pytest.raises(VoiceChatError):
        await vc.stop("g")


async def test_can_send_true_for_users():
    client = FakeClient(types.User(id=9))
    vc = VoiceChatController(client)
    assert await vc.can_send("someone") is True


async def test_can_send_respects_chat_default_ban():
    vc = VoiceChatController(FakeClient(make_chat(default_banned_send=True)))
    assert await vc.can_send("g") is False
    vc = VoiceChatController(FakeClient(make_chat(default_banned_send=False)))
    assert await vc.can_send("g") is True


async def test_can_send_banned_channel_participant():
    rights = types.ChatBannedRights(until_date=None, send_messages=True)
    banned = types.ChannelParticipantBanned(
        peer=types.PeerUser(7), kicked_by=1, date=None, banned_rights=rights
    )
    client = FakeClient(make_channel(), participant=banned)
    vc = VoiceChatController(client)
    assert await vc.can_send("c") is False


async def test_can_send_plain_megagroup_member():
    member = types.ChannelParticipant(user_id=7, date=None)
    client = FakeClient(make_channel(), participant=member)
    vc = VoiceChatController(client)
    assert await vc.can_send("c") is True


async def test_can_send_broadcast_non_admin_is_false():
    member = types.ChannelParticipant(user_id=7, date=None)
    client = FakeClient(make_channel(broadcast=True), participant=member)
    vc = VoiceChatController(client)
    assert await vc.can_send("c") is False


async def test_send_message_blocked_raises_and_sends_nothing():
    client = FakeClient(make_chat(default_banned_send=True))
    vc = VoiceChatController(client)
    with pytest.raises(VoiceChatError):
        await vc.send_message("g", "hello")
    assert client.sent == []


async def test_send_message_allowed_sends():
    client = FakeClient(make_chat(default_banned_send=False))
    vc = VoiceChatController(client)
    await vc.send_message("g", "hello")
    assert client.sent and client.sent[0][1] == "hello"
