"""Tests for the Group Membership Audit.

The fakes mimic Telethon's ChatAction.Event shape: the classifying booleans
(user_added / user_joined / user_left / user_kicked) plus the async entity
getters (get_users / get_chat / get_added_by). No Telethon or network is used.
"""

from __future__ import annotations

import json
import os
import stat

from parlay.membership.events import Actor, Chat, MembershipAction, MembershipEvent
from parlay.membership.notifier import MembershipNotifier
from parlay.membership.store import AuditLogStore
from parlay.membership.watcher import MembershipWatcher


class FakeEntity:
    """Stand-in for a resolved Telethon User/Chat entity."""

    def __init__(self, entity_id: int, *, username: str | None = None, title: str | None = None):
        self.id = entity_id
        self.username = username
        self.title = title


class FakeChatAction:
    """Stand-in for events.ChatAction.Event with the attributes we read."""

    def __init__(
        self,
        *,
        user_added: bool = False,
        user_joined: bool = False,
        user_left: bool = False,
        user_kicked: bool = False,
        users: list[FakeEntity] | None = None,
        chat: FakeEntity | None = None,
        added_by: FakeEntity | None = None,
    ) -> None:
        self.user_added = user_added
        self.user_joined = user_joined
        self.user_left = user_left
        self.user_kicked = user_kicked
        self._users = users or []
        self._chat = chat
        self._added_by = added_by
        self.chat_id = chat.id if chat else None
        self.user_id = self._users[0].id if self._users else None

    async def get_users(self) -> list[FakeEntity]:
        return self._users

    async def get_chat(self) -> FakeEntity | None:
        return self._chat

    async def get_added_by(self) -> FakeEntity | None:
        return self._added_by


def _collector() -> tuple[list[MembershipEvent], object]:
    seen: list[MembershipEvent] = []

    async def sink(event: MembershipEvent) -> None:
        seen.append(event)

    return seen, sink


async def test_added_by_other_captures_chat_and_adder() -> None:
    seen, sink = _collector()
    watcher = MembershipWatcher("100", [sink])
    event = await watcher.handle(
        FakeChatAction(
            user_added=True,
            users=[FakeEntity(100)],
            chat=FakeEntity(-500, title="Ops Room"),
            added_by=FakeEntity(7, username="alice"),
        )
    )
    assert event is not None
    assert event.action is MembershipAction.ADDED
    assert event.chat.chat_id == "-500"
    assert event.chat.title == "Ops Room"
    assert event.adder.user_id == "7"
    assert event.adder.name == "alice"
    assert seen == [event]


async def test_self_join_records_add_with_unknown_adder() -> None:
    seen, sink = _collector()
    watcher = MembershipWatcher("100", [sink])
    # A voluntary join carries no added_by (AC-MEM-001.2).
    event = await watcher.handle(
        FakeChatAction(user_joined=True, users=[FakeEntity(100)], chat=FakeEntity(-1))
    )
    assert event is not None
    assert event.action is MembershipAction.ADDED
    assert not event.adder.is_known
    assert seen == [event]


async def test_removal_is_recorded() -> None:
    seen, sink = _collector()
    watcher = MembershipWatcher("100", [sink])
    event = await watcher.handle(
        FakeChatAction(user_left=True, users=[FakeEntity(100)], chat=FakeEntity(-1))
    )
    assert event is not None
    assert event.action is MembershipAction.REMOVED
    assert seen == [event]


async def test_kick_of_self_is_recorded_as_removal() -> None:
    seen, sink = _collector()
    watcher = MembershipWatcher("100", [sink])
    event = await watcher.handle(
        FakeChatAction(user_kicked=True, users=[FakeEntity(100)], chat=FakeEntity(-1))
    )
    assert event is not None
    assert event.action is MembershipAction.REMOVED


async def test_action_for_other_user_is_ignored() -> None:
    seen, sink = _collector()
    watcher = MembershipWatcher("100", [sink])
    event = await watcher.handle(
        FakeChatAction(user_added=True, users=[FakeEntity(999)], chat=FakeEntity(-1))
    )
    assert event is None
    assert seen == []


async def test_non_membership_action_is_ignored() -> None:
    seen, sink = _collector()
    watcher = MembershipWatcher("100", [sink])
    # No add/remove flags set (e.g. a title change): must not emit.
    event = await watcher.handle(FakeChatAction(users=[FakeEntity(100)], chat=FakeEntity(-1)))
    assert event is None
    assert seen == []


async def test_watcher_fans_out_to_all_sinks() -> None:
    seen_a, sink_a = _collector()
    seen_b, sink_b = _collector()
    watcher = MembershipWatcher("100", [sink_a, sink_b])
    await watcher.handle(
        FakeChatAction(user_added=True, users=[FakeEntity(100)], chat=FakeEntity(-1))
    )
    assert len(seen_a) == 1
    assert len(seen_b) == 1


async def test_notifier_names_chat_and_adder() -> None:
    sent: list[str] = []

    async def send_private(text: str) -> None:
        sent.append(text)

    notifier = MembershipNotifier(send_private)
    await notifier(
        MembershipEvent.added(Chat("-500", "Ops Room"), Actor(user_id="7", name="alice"))
    )
    assert len(sent) == 1
    assert "Ops Room" in sent[0]
    assert "alice" in sent[0]


async def test_notifier_states_unknown_adder() -> None:
    sent: list[str] = []

    async def send_private(text: str) -> None:
        sent.append(text)

    notifier = MembershipNotifier(send_private)
    await notifier(MembershipEvent.added(Chat("-1"), Actor.unknown()))
    assert "could not be determined" in sent[0]


async def test_audit_log_appends_json_lines_owner_only(tmp_path) -> None:
    path = tmp_path / "nested" / "audit.log"
    store = AuditLogStore(path)
    await store.append(MembershipEvent.added(Chat("-500", "Ops"), Actor("7", "alice")))
    await store.append(MembershipEvent.removed(Chat("-500", "Ops")))

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["action"] == "added"
    assert first["chat_id"] == "-500"
    assert first["adder_id"] == "7"
    second = json.loads(lines[1])
    assert second["action"] == "removed"
    assert second["adder_id"] is None
    # AC-MEM-003.2: the log is owner-only.
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600


def test_actor_unknown_display() -> None:
    assert not Actor.unknown().is_known
    assert Actor.unknown().display == "an unknown user"
    assert Actor("7", "alice").display == "alice (7)"
    assert Actor("7").display == "user 7"
