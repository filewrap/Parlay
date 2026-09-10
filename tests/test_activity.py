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
    *,
    left: bool = False,
    muted: bool = False,
    can_self_unmute: bool = True,
    just_joined: bool = False,
) -> types.GroupCallParticipant:
    return types.GroupCallParticipant(
        peer=types.PeerUser(7),
        date=None,
        source=42,
        left=left,
        muted=muted,
        can_self_unmute=can_self_unmute,
        just_joined=just_joined,
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
    if tracker._discovery_task is not None:
        await tracker._discovery_task
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
    if tracker._discovery_task is not None:
        await tracker._discovery_task
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
    if tracker._discovery_task is not None:
        await tracker._discovery_task
    try:
        client.participants[11] = []
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
    if tracker._discovery_task is not None:
        await tracker._discovery_task
    try:
        await tracker.set_transport(chat_id, True)
        update = types.UpdateGroupCall(
            call=types.GroupCallDiscarded(id=11, access_hash=110, duration=8),
            peer=types.PeerChannel(channel_id=101),
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
    if tracker._discovery_task is not None:
        await tracker._discovery_task
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
    if tracker._discovery_task is not None:
        await tracker._discovery_task
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
    if tracker._discovery_task is not None:
        await tracker._discovery_task
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
    if tracker._discovery_task is not None:
        await tracker._discovery_task
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


async def test_concurrent_transport_and_participant_save_merge_fields(tmp_path) -> None:
    client = FakeClient()
    chat_id = setup_chat(client, 101, 11)
    _seen, callback = callback_collector()
    tracker = ActivityTracker(client, tmp_path / "activity.db", 7, callback)
    await tracker.start()
    if tracker._discovery_task is not None:
        await tracker._discovery_task
    try:
        previous = await tracker._get(chat_id)
        await asyncio.gather(
            tracker.set_transport(chat_id, True),
            tracker._apply_self(chat_id, participant(), 2, previous),
        )
        state = await tracker._get(chat_id)
        assert state is not None
        assert state.transport is True
        assert state.membership == "joined"
        assert state.version == 2
    finally:
        await tracker.stop()


async def test_send_only_restriction_is_not_membership_removal(tmp_path) -> None:
    client = FakeClient()
    _seen, callback = callback_collector()
    tracker = ActivityTracker(client, tmp_path / "activity.db", 7, callback)
    await tracker.start()
    chat_id = get_peer_id(types.PeerChannel(101))
    scheduled: list[int] = []
    tracker._schedule_reconcile = scheduled.append
    try:
        await tracker._save(chat_id, call_state="active", membership="joined", transport=True)
        restricted = types.ChannelParticipantBanned(
            peer=types.PeerUser(7),
            kicked_by=1,
            date=None,
            banned_rights=types.ChatBannedRights(until_date=None, send_messages=True),
        )
        update = SimpleNamespace(channel_id=101, user_id=7, new_participant=restricted)
        await tracker._handle_channel_participant(update)
        state = await tracker._get(chat_id)
        assert state is not None
        assert state.membership == "joined"
        assert state.transport is True
        assert scheduled == [chat_id]
    finally:
        await tracker.stop()


async def test_stale_discarded_call_does_not_overwrite_new_call(tmp_path) -> None:
    client = FakeClient()
    chat_id = setup_chat(client, 101, 11)
    client.participants[11] = [participant()]
    seen, callback = callback_collector()
    tracker = ActivityTracker(client, tmp_path / "activity.db", 7, callback)
    await tracker.start()
    if tracker._discovery_task is not None:
        await tracker._discovery_task
    try:
        await tracker._save(
            chat_id,
            call_id=22,
            access_hash=220,
            call_state="active",
            membership="joined",
            transport=True,
            version=5,
        )
        stale = types.UpdateGroupCall(
            call=types.GroupCallDiscarded(id=11, access_hash=110, duration=8),
            peer=types.PeerChat(chat_id=101),
        )
        await tracker.handle_update(stale)
        state = await tracker._get(chat_id)
        assert state is not None
        assert state.call_id == 22
        assert state.call_state == "active"
        assert state.membership == "joined"
        assert state.transport is True
        assert seen == []
    finally:
        await tracker.stop()


async def test_stale_group_call_version_does_not_regress_state(tmp_path) -> None:
    client = FakeClient()
    chat_id = setup_chat(client, 101, 11)
    _seen, callback = callback_collector()
    tracker = ActivityTracker(client, tmp_path / "activity.db", 7, callback)
    await tracker.start()
    if tracker._discovery_task is not None:
        await tracker._discovery_task
    try:
        await tracker._save(chat_id, version=5)
        stale = SimpleNamespace(
            call=SimpleNamespace(id=11, access_hash=110, version=4),
            peer=types.PeerChat(chat_id=101),
        )
        await tracker._handle_group_call(stale)
        state = await tracker._get(chat_id)
        assert state is not None
        assert state.version == 5
    finally:
        await tracker.stop()


async def test_start_returns_before_discovery_rpc(tmp_path) -> None:
    client = FakeClient()
    setup_chat(client, 101, 11)
    gate = asyncio.Event()

    async def blocked_dialogs():
        client.dialog_iterations += 1
        await gate.wait()
        if False:
            yield None

    client.iter_dialogs = blocked_dialogs
    _seen, callback = callback_collector()
    tracker = ActivityTracker(client, tmp_path / "activity.db", 7, callback)
    await asyncio.wait_for(tracker.start(), timeout=1)
    try:
        assert tracker._discovery_task is not None
        assert not tracker._discovery_task.done()
    finally:
        gate.set()
        await tracker.stop()


async def test_start_discovery_does_not_duplicate_reconcile(tmp_path) -> None:
    client = FakeClient()
    setup_chat(client, 101, 11)
    _seen, callback = callback_collector()
    tracker = ActivityTracker(client, tmp_path / "activity.db", 7, callback)
    await tracker.start()
    try:
        assert tracker._discovery_task is not None
        await tracker._discovery_task
        full = sum(
            isinstance(item, functions.messages.GetFullChatRequest) for item in client.requests
        )
        participants = sum(
            isinstance(item, functions.phone.GetGroupParticipantsRequest)
            for item in client.requests
        )
        assert full == 1
        assert participants == 1
    finally:
        await tracker.stop()


async def test_failed_discovery_retries_only_after_retry_interval(tmp_path) -> None:
    client = FakeClient()

    async def failed_dialogs():
        client.dialog_iterations += 1
        raise RuntimeError("dialogs unavailable")
        yield

    client.iter_dialogs = failed_dialogs
    _seen, callback = callback_collector()
    tracker = ActivityTracker(client, tmp_path / "activity.db", 7, callback)
    tracker._DISCOVERY_RETRY_SECONDS = 0
    tracker._RECONCILE_SECONDS = 0.01
    await tracker.start()
    try:
        assert tracker._discovery_task is not None
        await tracker._discovery_task
        assert tracker._discovery_failed is True
        await asyncio.sleep(0.03)
        assert client.dialog_iterations >= 2
    finally:
        await tracker.stop()


async def test_left_and_just_joined_use_version_rules(tmp_path) -> None:
    client = FakeClient()
    chat_id = setup_chat(client, 101, 11)
    client.participants[11] = [participant()]
    _seen, callback = callback_collector()
    tracker = ActivityTracker(client, tmp_path / "activity.db", 7, callback)
    await tracker.start()
    if tracker._discovery_task is not None:
        await tracker._discovery_task
    try:
        await tracker._save(chat_id, version=5, membership="joined")
        await tracker.handle_update(
            types.UpdateGroupCallParticipants(
                call=types.InputGroupCall(id=11, access_hash=110),
                participants=[participant(left=True)],
                version=4,
            )
        )
        state = await tracker._get(chat_id)
        assert state is not None and state.membership == "joined"
        await tracker.handle_update(
            types.UpdateGroupCallParticipants(
                call=types.InputGroupCall(id=11, access_hash=110),
                participants=[participant(just_joined=True)],
                version=7,
            )
        )
        assert chat_id in tracker._reconcile_tasks
    finally:
        await tracker.stop()


async def test_old_active_call_schedules_reconcile_without_replacing(tmp_path) -> None:
    client = FakeClient()
    chat_id = setup_chat(client, 101, 11)
    _seen, callback = callback_collector()
    tracker = ActivityTracker(client, tmp_path / "activity.db", 7, callback)
    await tracker.start()
    if tracker._discovery_task is not None:
        await tracker._discovery_task
    scheduled: list[int] = []
    tracker._schedule_reconcile = scheduled.append
    try:
        await tracker._save(chat_id, call_id=22, access_hash=220, version=5)
        stale = SimpleNamespace(
            call=SimpleNamespace(id=11, access_hash=110, version=6),
            peer=types.PeerChat(chat_id=101),
        )
        await tracker._handle_group_call(stale)
        state = await tracker._get(chat_id)
        assert state is not None and state.call_id == 22
        assert scheduled == [chat_id]
    finally:
        await tracker.stop()
