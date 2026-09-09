"""MembershipWatcher: turn MTProto chat-action updates into MembershipEvents.

The watcher observes the same user session as the rest of Parlay (no new
container). It reads the update object with tolerant `getattr` access rather
than importing Telethon types, so it stays provider-agnostic and unit testable:
a test can feed a simple stand-in object with the same attribute shape.

It reacts only to actions involving Parlay's own account. When the account is
added it captures the adder if the event carries one and marks it unknown
otherwise (REQ-MEM-001); when the account leaves or is removed it records a
removal. Each event is fanned out to every registered sink (the notifier and
the audit log).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence

from .events import Actor, Chat, MembershipEvent

log = logging.getLogger(__name__)

# A consumer of membership events (the notifier, the audit log store).
EventSink = Callable[[MembershipEvent], Awaitable[None]]


def _as_id(value: object) -> str | None:
    """Coerce a user/chat id or entity into a string id, or None."""
    if value is None:
        return None
    if isinstance(value, int | str):
        text = str(value).strip()
        return text or None
    ident = getattr(value, "id", None)
    return str(ident) if ident is not None else None


def _entity_name(value: object) -> str | None:
    """Best-effort display name from an entity object."""
    if value is None or isinstance(value, int | str):
        return None
    for attr in ("username", "title", "first_name"):
        name = getattr(value, attr, None)
        if name:
            return str(name)
    return None


class MembershipWatcher:
    """Detects add/remove of Parlay's account and emits MembershipEvents."""

    def __init__(self, self_id: str, sinks: Sequence[EventSink]) -> None:
        self._self_id = str(self_id)
        self._sinks = list(sinks)

    async def handle(self, action: object) -> MembershipEvent | None:
        """Inspect one chat-action update; emit an event if it involves us.

        Returns the emitted event, or None when the update does not concern
        Parlay's own account.
        """
        if not self._involves_self(action):
            return None
        chat = self._chat(action)
        if self._is_removal(action):
            event: MembershipEvent = MembershipEvent.removed(chat)
        else:
            event = MembershipEvent.added(chat, self._adder(action))
        await self._emit(event)
        return event

    # --- parsing --------------------------------------------------------------
    def _affected_ids(self, action: object) -> list[str]:
        """Ids of the users this action added/removed."""
        ids: list[str] = []
        raw_ids = getattr(action, "user_ids", None)
        if isinstance(raw_ids, Iterable) and not isinstance(raw_ids, str | bytes):
            ids.extend(i for i in (_as_id(v) for v in raw_ids) if i is not None)
        users = getattr(action, "users", None)
        if isinstance(users, Iterable) and not isinstance(users, str | bytes):
            ids.extend(i for i in (_as_id(v) for v in users) if i is not None)
        single = _as_id(getattr(action, "user_id", None))
        if single is not None:
            ids.append(single)
        return ids

    def _involves_self(self, action: object) -> bool:
        return self._self_id in self._affected_ids(action)

    def _is_removal(self, action: object) -> bool:
        return bool(getattr(action, "user_left", False) or getattr(action, "user_kicked", False))

    def _chat(self, action: object) -> Chat:
        chat_id = _as_id(getattr(action, "chat_id", None)) or _as_id(getattr(action, "chat", None))
        title = _entity_name(getattr(action, "chat", None))
        return Chat(chat_id=chat_id or "unknown", title=title)

    def _adder(self, action: object) -> Actor:
        added_by = getattr(action, "added_by", None)
        adder_id = _as_id(added_by)
        if adder_id is None:
            return Actor.unknown()
        return Actor(user_id=adder_id, name=_entity_name(added_by))

    # --- fan-out --------------------------------------------------------------
    async def _emit(self, event: MembershipEvent) -> None:
        for sink in self._sinks:
            try:
                await sink(event)
            except Exception:
                # A failing sink (e.g. notification send) must not stop the
                # others (e.g. the durable audit log).
                log.exception("membership event sink failed")
