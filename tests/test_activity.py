"""Tests for durable, Telethon-native activity tracking."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from telethon.tl import functions, types
from telethon.utils import get_peer_id

from parlay.activity import ActivityTracker


def make_chat(chat_id: int) -> types.Chat:
    return types.Chat(
        id=chat_id,
        title=f"chat-{chat_id}",
        photo=types.ChatPhotoEmpty(),
        participants_count=2,
        date=None,
        version=1,
    )


def participant(
    *, left: bool = False, muted: bool = False, can_self_unmute: bool = True
) -> types.GroupCallParticipant:
    return types.GroupCallParticipant(
        peer=types.PeerUser(7),
        date=None,
        source=42,
        left=left,
        muted=muted,
        can_self_unmute=can_self_unmute,
        is_self=True,
    )


class FakeClient:
    def __init__(self) -> None:
        self.entities: dict[int, types.Chat] = {}
        self.calls: dict[int, types.InputGroupCall | None] = {}
        self.participants: dict[int, list[types.GroupCallParticipant]] = {}
        self.versions: dict[int, int] = {}
        self.requests: list[object] = []
        self.dialogs: list[object] = []
        self.fail_participants = False
        self.dialog_iterations = 0

    async def get_entity(self, chat_id: int) -> types.Chat:
        return self.entities[chat_id]

    async def get_input_entity(self, entity: object) -> object:
        return entity

    async def iter_dialogs(self):
        self.dialog_iterations += 1
        for dialog in self.dialogs:
            yield dialog

    async def __call__(self, request: object) -> object:
        self.requests.append(request)
        if isinstance(request, functions.messages.GetFullChatRequest):
            return SimpleNamespace(full_chat=SimpleNamespace(call=self.calls.get(request.chat_id)))
        if isinstance(request, functions.channels.GetFullChannelRequest):
            channel_id = int(getattr(request.channel, "channel_id", request.channel.id))
            return SimpleNamespace(full_chat=SimpleNamespace(call=self.calls.get(channel_id)))
        if isinstance(request, functions.phone.GetGroupParticipantsRequest):
            if self.fail_participants:
                raise RuntimeError("rpc unavailable")
            assert request.ids and isinstance(request.ids[0], types.InputPeerSelf)
            assert request.sources == [] and request.offset == "" and request.limit == 1
            return SimpleNamespace(
                participants=self.participants.get(request.call.id, []),
                version=self.versions.get(request.call.id, 1),
            )
        raise AssertionError(f"unexpected request: {type(request).__name__}")


def setup_chat(client: FakeClient, chat_id: int, call_id: int) -> int:
    chat = make_chat(chat_id)
    marked = get_peer_id(chat)
    client.entities[marked] = chat
    client.calls[chat_id] = types.InputGroupCall(id=call_id, access_hash=call_id * 10)
    client.participants[call_id] = []
    client.versions[call_id] = 1
    client.dialogs.append(SimpleNamespace(is_group=True, entity=chat))
    return marked


def callback_collector():
    seen: list[tuple[int, str]] = []

    async def callback(chat_id: int, reason: str) -> None:
        seen.append((chat_id, reason))

    return seen, callback


async def test_start_discovers_call_and_uses_targeted_self_request(tmp_path) -> None:
    client = FakeClient()
    chat_id = setup_chat(client, 101, 11)
    client.participants[11] = [participant()]
    seen, callback = callback_collector()
    tracker = ActivityTracker(client, tmp_path / "activity.db", 7, callback)
    await tracker.start()
    try:
        assert await tracker.status_text(chat_id) == (
            "Voice chat active; account joined; media transport disconnected."
        )
        assert seen == []
    finally:
        await tracker.stop()


async def test_absent_before_join_does_not_notify(tmp_path) -> None:
    client = FakeClient()
    chat_id = setup_chat(client, 101, 11)
    seen, callback = callback_collector()
    tracker = ActivityTracker(client, tmp_path / "activity.db", 7, callback)
    await tracker.start()
    try:
        assert "account not joined" in await tracker.status_text(chat_id)
        assert seen == []
    finally:
        await tracker.stop()


async def test_join_leave_and_duplicate_removal_are_deduplicated(tmp_path) -> None:
    client = FakeClient()
    chat_id = setup_chat(client, 101, 11)
    client.participants[11] = [participant()]
    seen, callback = callback_collector()
    tracker = ActivityTracker(client, tmp_path / "activity.db", 7, callback)
    await tracker.start()
    try:
        removal = types.UpdateGroupCallParticipants(
            call=types.InputGroupCall(id=11, access_hash=110),
            participants=[participant(left=True)],
            version=2,
        )
        await tracker.handle_update(removal)
        await tracker.handle_update(removal)
        pending = list(tracker._reconcile_tasks.values())
        if pending:
            await asyncio.gather(*pending)
        assert seen == [(chat_id, "self_removed")]
        assert "account not joined" in await tracker.status_text(chat_id)
    finally:
        await tracker.stop()


async def test_discard_clears_transport_and_notifies_once(tmp_path) -> None:
    client = FakeClient()
    chat_id = setup_chat(client, 101, 11)
    client.participants[11] = [participant()]
    seen, callback = callback_collector()
    tracker = ActivityTracker(client, tmp_path / "activity.db", 7, callback)
    await tracker.start()
    try:
        await tracker.set_transport(chat_id, True)
        update = types.UpdateGroupCall(
            call=types.GroupCallDiscarded(id=11, access_hash=110, duration=8),
            chat_id=101,
        )
        await tracker.handle_update(update)
        await tracker.handle_update(update)
        assert seen == [(chat_id, "call_discarded")]
        assert await tracker.status_text(chat_id) == (
            "No active voice chat; account not joined; media transport disconnected."
        )
    finally:
        await tracker.stop()


async def test_different_chats_are_isolated(tmp_path) -> None:
    client = FakeClient()
    first = setup_chat(client, 101, 11)
    second = setup_chat(client, 202, 22)
    client.participants[11] = [participant()]
    seen, callback = callback_collector()
    tracker = ActivityTracker(client, tmp_path / "activity.db", 7, callback)
    await tracker.start()
    try:
        await tracker.set_transport(first, True)
        assert "account joined" in await tracker.status_text(first)
        assert "transport connected" in await tracker.status_text(first)
        assert "account not joined" in await tracker.status_text(second)
        assert "transport disconnected" in await tracker.status_text(second)
        assert seen == []
    finally:
        await tracker.stop()


async def test_restart_persists_activity_but_clears_transport(tmp_path) -> None:
    path = tmp_path / "activity.db"
    client = FakeClient()
    chat_id = setup_chat(client, 101, 11)
    client.participants[11] = [participant()]
    seen, callback = callback_collector()
    first = ActivityTracker(client, path, 7, callback)
    await first.start()
    await first.set_transport(chat_id, True)
    await first.stop()

    client.fail_participants = True
    second = ActivityTracker(client, path, 7, callback)
    await second.start()
    try:
        status = await second.status_text(chat_id)
        assert "state unknown" in status
        assert "participation unknown" in status
        assert "transport disconnected" in status
        assert seen == []
    finally:
        await second.stop()


async def test_rpc_failure_marks_unknown_without_false_leave(tmp_path) -> None:
    client = FakeClient()
    chat_id = setup_chat(client, 101, 11)
    client.fail_participants = True
    seen, callback = callback_collector()
    tracker = ActivityTracker(client, tmp_path / "activity.db", 7, callback)
    await tracker.start()
    try:
        assert await tracker.status_text(chat_id) == (
            "Voice chat state unknown; account participation unknown; media transport disconnected."
        )
        assert seen == []
    finally:
        await tracker.stop()


async def test_version_gap_coalesces_reconciliation(tmp_path) -> None:
    client = FakeClient()
    chat_id = setup_chat(client, 101, 11)
    client.versions[11] = 5
    seen, callback = callback_collector()
    tracker = ActivityTracker(client, tmp_path / "activity.db", 7, callback)
    await tracker.start()
    try:
        before = sum(
            isinstance(item, functions.phone.GetGroupParticipantsRequest)
            for item in client.requests
        )
        gap = types.UpdateGroupCallParticipants(
            call=types.InputGroupCall(id=11, access_hash=110),
            participants=[],
            version=9,
        )
        await asyncio.gather(tracker.handle_update(gap), tracker.handle_update(gap))
        await asyncio.sleep(0)
        pending = list(tracker._reconcile_tasks.values())
        if pending:
            await asyncio.gather(*pending)
        after = sum(
            isinstance(item, functions.phone.GetGroupParticipantsRequest)
            for item in client.requests
        )
        assert after - before == 1
        assert seen == []
        assert "account not joined" in await tracker.status_text(chat_id)
    finally:
        await tracker.stop()


async def test_unknown_call_updates_coalesce_dialog_discovery(tmp_path) -> None:
    client = FakeClient()
    seen, callback = callback_collector()
    tracker = ActivityTracker(client, tmp_path / "activity.db", 7, callback)
    await tracker.start()
    baseline = client.dialog_iterations
    try:
        unknown = types.UpdateGroupCallParticipants(
            call=types.InputGroupCall(id=999, access_hash=1),
            participants=[],
            version=1,
        )
        await asyncio.gather(tracker.handle_update(unknown), tracker.handle_update(unknown))
        await asyncio.sleep(0)
        if tracker._discovery_task is not None:
            await tracker._discovery_task
        assert client.dialog_iterations == baseline + 1
        assert seen == []
    finally:
        await tracker.stop()


async def test_media_revoke_notifies_once_and_recovery_clears_reason(tmp_path) -> None:
    client = FakeClient()
    chat_id = setup_chat(client, 101, 11)
    client.participants[11] = [participant()]
    seen, callback = callback_collector()
    tracker = ActivityTracker(client, tmp_path / "activity.db", 7, callback)
    await tracker.start()
    try:
        revoked = types.UpdateGroupCallParticipants(
            call=types.InputGroupCall(id=11, access_hash=110),
            participants=[participant(muted=True, can_self_unmute=False)],
            version=2,
        )
        await tracker.handle_update(revoked)
        await tracker.handle_update(revoked)
        pending = list(tracker._reconcile_tasks.values())
        if pending:
            await asyncio.gather(*pending)
        assert seen == [(chat_id, "media_revoked")]
        client.participants[11] = [participant()]
        client.versions[11] = 3
        await tracker.reconcile(chat_id)
        assert "account joined" in await tracker.status_text(chat_id)
    finally:
        await tracker.stop()


async def test_public_methods_require_start_and_stop_is_idempotent(tmp_path) -> None:
    client = FakeClient()
    _seen, callback = callback_collector()
    tracker = ActivityTracker(client, tmp_path / "activity.db", 7, callback)
    with pytest.raises(RuntimeError, match="not started"):
        await tracker.status_text(-101)
    await tracker.start()
    await tracker.stop()
    await tracker.stop()
