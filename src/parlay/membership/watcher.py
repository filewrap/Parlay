"""MembershipWatcher: turn Telethon ChatAction updates into MembershipEvents.

The watcher observes the same user session as the rest of Parlay (no new
container). It is registered as a `events.ChatAction` handler in the app; that
builder fires on every member change in a chat, so the watcher must gate on the
real event flags rather than infer intent from ids.

Telethon `ChatAction.Event` contract used here (verified against the docs):
  * Booleans `user_added` (added by someone else), `user_joined` (self-join),
    `user_left`, and `user_kicked` classify the action.
  * `await event.get_chat()` resolves the chat; `chat_id` is the marked id.
  * `await event.get_added_by()` resolves the adder `User` or None.
  * `await event.get_users()` / `user_ids` give the affected users; `user_id`
    is the first one. Telethon requires the async getters to resolve entities
    reliably, so we prefer them and fall back to the plain attributes.

The watcher acts only when Parlay's own account is one of the affected users:
an add (added or self-joined) captures the adder, unknown when Telegram did not
carry one (REQ-MEM-001); a leave/kick records a removal. Each event fans out to
every registered sink (the notifier and the audit log). The module imports no
Telethon types, so a test can pass a stand-in event with the same shape.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable, Sequence

from .events import Actor, Chat, MembershipEvent

log = logging.getLogger(__name__)

# A consumer of membership events (the notifier, the audit log store).
EventSink = Callable[[MembershipEvent], Awaitable[None]]


async def _call(obj: object, name: str) -> object | None:
    """Call an optional async getter (e.g. get_chat) and return its result.

    Telethon exposes entity resolution through async methods; when the method
    is missing or raises we fall back to None so a bare attribute can be used.
    """
    method = getattr(obj, name, None)
    if not callable(method):
        return None
    try:
        result = method()
        if isinstance(result, Awaitable):
            return await result
        return result
    except Exception:
        log.debug("ChatAction getter %s failed", name, exc_info=True)
        return None


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
    """Best-effort display name from a resolved User/Chat entity."""
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

    async def handle(self, event: object) -> MembershipEvent | None:
        """Inspect one ChatAction event; emit if it involves Parlay's account.

        Returns the emitted event, or None when the update is not an add/remove
        of the account, or does not concern it.
        """
        added = bool(getattr(event, "user_added", False) or getattr(event, "user_joined", False))
        removed = bool(getattr(event, "user_left", False) or getattr(event, "user_kicked", False))
        if not (added or removed):
            return None
        if not await self._involves_self(event):
            return None
        chat = await self._chat(event)
        # A leave/kick is a removal even if the same event also set an add flag;
        # removals win because they release the account from the chat.
        if removed:
            membership: MembershipEvent = MembershipEvent.removed(chat)
        else:
            membership = MembershipEvent.added(chat, await self._adder(event))
        await self._emit(membership)
        return membership

    # --- parsing --------------------------------------------------------------
    async def _affected_ids(self, event: object) -> list[str]:
        """Ids of the users this action added/removed, via getter then attrs."""
        ids: list[str] = []
        users = await _call(event, "get_users")
        if isinstance(users, Iterable) and not isinstance(users, str | bytes):
            ids.extend(i for i in (_as_id(v) for v in users) if i is not None)
        if not ids:
            raw_ids = getattr(event, "user_ids", None)
            if isinstance(raw_ids, Iterable) and not isinstance(raw_ids, str | bytes):
                ids.extend(i for i in (_as_id(v) for v in raw_ids) if i is not None)
        single = _as_id(getattr(event, "user_id", None))
        if single is not None and single not in ids:
            ids.append(single)
        return ids

    async def _involves_self(self, event: object) -> bool:
        return self._self_id in await self._affected_ids(event)

    async def _chat(self, event: object) -> Chat:
        entity = await _call(event, "get_chat")
        chat_id = _as_id(entity) or _as_id(getattr(event, "chat_id", None))
        title = _entity_name(entity)
        return Chat(chat_id=chat_id or "unknown", title=title)

    async def _adder(self, event: object) -> Actor:
        added_by = await _call(event, "get_added_by")
        if added_by is None:
            added_by = getattr(event, "added_by", None)
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
