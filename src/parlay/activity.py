"""Durable, account-local Telegram voice-chat activity tracking.

Raw group-call updates are hints, not an exhaustive event stream. Telegram sends
partial participant updates and version gaps are possible, so this module
refetches a targeted self snapshot when needed. All SQLite calls run in worker
threads to keep database I/O off the media event loop.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from telethon.tl import functions, types
from telethon.utils import get_peer_id

log = logging.getLogger(__name__)
UnavailableCallback = Callable[[int, str], Awaitable[None]]
_T = TypeVar("_T")
_UNSET = object()


@dataclass(frozen=True)
class _Activity:
    chat_id: int
    call_id: int | None
    access_hash: int | None
    call_state: str
    membership: str
    transport: bool
    version: int | None
    unavailable_reason: str | None


class ActivityTracker:
    """Track current call, own participation, and media transport per chat.

    ``on_unavailable`` is called with ``call_discarded``, ``self_removed``,
    ``media_revoked``, or ``membership_removed``. Absence before this account
    has joined a call does not trigger it.
    """

    _RECONCILE_SECONDS = 60.0
    _DISCOVERY_CONCURRENCY = 4

    def __init__(
        self,
        client: Any,
        db_path: str | Path,
        account_id: str | int,
        on_unavailable: UnavailableCallback,
    ) -> None:
        self._client = client
        self._db_path = str(db_path)
        self._account_id = str(account_id)
        self._on_unavailable = on_unavailable
        self._connection: sqlite3.Connection | None = None
        self._db_lock = threading.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._started = False
        self._stopping = False
        self._periodic_task: asyncio.Task[None] | None = None
        self._reconcile_tasks: dict[int, asyncio.Task[None]] = {}
        self._discovery_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Open storage, clear stale transport, discover calls, and reconcile."""
        async with self._lifecycle_lock:
            if self._started:
                return
            self._stopping = False
            await asyncio.to_thread(self._open_database)
            await self._execute(
                "UPDATE activity SET transport=0, updated_at=? WHERE account_id=?",
                (int(time.time()), self._account_id),
            )
            self._started = True
        await self._discover_dialogs()
        for chat_id in await self._known_active_chats():
            await self._reconcile_safely(chat_id)
        if self._started and not self._stopping:
            self._periodic_task = asyncio.create_task(
                self._periodic_reconcile(), name="parlay-activity-periodic"
            )

    async def stop(self) -> None:
        """Cancel background work and close the database safely."""
        async with self._lifecycle_lock:
            if not self._started and self._connection is None:
                return
            self._stopping = True
            tasks = set(self._reconcile_tasks.values())
            if self._discovery_task is not None:
                tasks.add(self._discovery_task)
            if self._periodic_task is not None:
                tasks.add(self._periodic_task)
            current = asyncio.current_task()
            for task in tasks:
                if task is not current:
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._reconcile_tasks.clear()
            self._discovery_task = None
            self._periodic_task = None
            self._started = False
            await asyncio.to_thread(self._close_database)
            self._stopping = False

    async def handle_update(self, update: Any) -> None:
        """Consume a raw Telethon update or a Telethon ChatAction event."""
        self._require_started()
        if isinstance(update, types.UpdateGroupCall):
            await self._handle_group_call(update)
        elif isinstance(update, types.UpdateGroupCallParticipants):
            await self._handle_participants(update)
        elif isinstance(update, types.UpdateChannel):
            self._schedule_reconcile(get_peer_id(types.PeerChannel(update.channel_id)))
        elif type(update).__name__ == "UpdateChannelParticipant":
            await self._handle_channel_participant(update)
        elif any(
            bool(getattr(update, field, False))
            for field in ("user_added", "user_joined", "user_left", "user_kicked")
        ):
            await self._handle_chat_action(update)

    async def reconcile(self, chat_id: int) -> None:
        """Fetch a chat's active call and trusted targeted own participant."""
        self._require_started()
        chat_id = self._normalise_chat_id(chat_id)
        try:
            entity = await self._client.get_entity(chat_id)
            active = await self._get_active_call(entity)
            previous = await self._get(chat_id)
            if active is None:
                was_active = previous is not None and previous.call_state == "active"
                await self._save(
                    chat_id,
                    call_state="inactive",
                    membership="left",
                    transport=False,
                    unavailable_reason="call_discarded" if was_active else None,
                )
                if was_active:
                    await self._notify_once(chat_id, "call_discarded", previous)
                return
            call = self._as_input_call(active)
            call_changed = previous is not None and previous.call_id not in (None, call.id)
            await self._save(
                chat_id,
                call_id=call.id,
                access_hash=call.access_hash,
                call_state="active",
                membership="unknown" if call_changed else _UNSET,
                version=None if call_changed else _UNSET,
                transport=False if call_changed else _UNSET,
                unavailable_reason=None if call_changed else _UNSET,
            )
            await self._fetch_self(chat_id, call)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("activity reconciliation failed for chat %s", chat_id, exc_info=True)
            await self._save(chat_id, call_state="unknown", membership="unknown")

    async def set_transport(self, chat_id: int, connected: bool) -> None:
        """Set native transport state. Connected state is cleared on restart."""
        self._require_started()
        await self._save(self._normalise_chat_id(chat_id), transport=connected)

    async def status_text(self, chat_id: int) -> str:
        """Return concise current activity for command presentation."""
        self._require_started()
        state = await self._get(self._normalise_chat_id(chat_id))
        if state is None:
            return "No activity recorded for this chat."
        call = {
            "active": "Voice chat active",
            "inactive": "No active voice chat",
            "unknown": "Voice chat state unknown",
        }.get(state.call_state, "Voice chat state unknown")
        member = {
            "joined": "account joined",
            "left": "account not joined",
            "unknown": "account participation unknown",
        }.get(state.membership, "account participation unknown")
        transport = (
            "media transport connected" if state.transport else "media transport disconnected"
        )
        return f"{call}; {member}; {transport}."

    def _open_database(self) -> None:
        path = Path(self._db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, check_same_thread=False)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS activity (
                account_id TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                call_id INTEGER,
                access_hash INTEGER,
                call_state TEXT NOT NULL DEFAULT 'unknown',
                membership TEXT NOT NULL DEFAULT 'unknown',
                transport INTEGER NOT NULL DEFAULT 0,
                version INTEGER,
                unavailable_reason TEXT,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (account_id, chat_id)
            )
            """
        )
        connection.commit()
        self._connection = connection

    def _close_database(self) -> None:
        with self._db_lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    async def _db(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        def run() -> _T:
            with self._db_lock:
                if self._connection is None:
                    raise RuntimeError("ActivityTracker database is closed")
                return operation(self._connection)

        return await asyncio.to_thread(run)

    async def _execute(self, sql: str, parameters: tuple[Any, ...]) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            connection.execute(sql, parameters)
            connection.commit()

        await self._db(operation)

    async def _save(
        self,
        chat_id: int,
        *,
        call_id: Any = _UNSET,
        access_hash: Any = _UNSET,
        call_state: Any = _UNSET,
        membership: Any = _UNSET,
        transport: Any = _UNSET,
        version: Any = _UNSET,
        unavailable_reason: Any = _UNSET,
    ) -> None:
        previous = await self._get(chat_id)

        def value(field: str, supplied: Any, default: Any) -> Any:
            if supplied is not _UNSET:
                return supplied
            return getattr(previous, field) if previous is not None else default

        values = (
            self._account_id,
            chat_id,
            value("call_id", call_id, None),
            value("access_hash", access_hash, None),
            value("call_state", call_state, "unknown"),
            value("membership", membership, "unknown"),
            int(bool(value("transport", transport, False))),
            value("version", version, None),
            value("unavailable_reason", unavailable_reason, None),
            int(time.time()),
        )
        await self._execute(
            """
            INSERT INTO activity (
                account_id, chat_id, call_id, access_hash, call_state, membership,
                transport, version, unavailable_reason, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_id, chat_id) DO UPDATE SET
                call_id=excluded.call_id,
                access_hash=excluded.access_hash,
                call_state=excluded.call_state,
                membership=excluded.membership,
                transport=excluded.transport,
                version=excluded.version,
                unavailable_reason=excluded.unavailable_reason,
                updated_at=excluded.updated_at
            """,
            values,
        )

    @staticmethod
    def _row(row: tuple[Any, ...] | None) -> _Activity | None:
        if row is None:
            return None
        return _Activity(*row[:5], bool(row[5]), *row[6:])

    async def _get(self, chat_id: int) -> _Activity | None:
        def operation(connection: sqlite3.Connection) -> _Activity | None:
            row = connection.execute(
                """
                SELECT chat_id, call_id, access_hash, call_state, membership,
                       transport, version, unavailable_reason
                FROM activity WHERE account_id=? AND chat_id=?
                """,
                (self._account_id, chat_id),
            ).fetchone()
            return self._row(row)

        return await self._db(operation)

    async def _find_by_call(self, call_id: int) -> _Activity | None:
        def operation(connection: sqlite3.Connection) -> _Activity | None:
            row = connection.execute(
                """
                SELECT chat_id, call_id, access_hash, call_state, membership,
                       transport, version, unavailable_reason
                FROM activity WHERE account_id=? AND call_id=?
                """,
                (self._account_id, call_id),
            ).fetchone()
            return self._row(row)

        return await self._db(operation)

    async def _known_active_chats(self) -> list[int]:
        def operation(connection: sqlite3.Connection) -> list[int]:
            rows = connection.execute(
                """
                SELECT chat_id FROM activity
                WHERE account_id=? AND call_state IN ('active', 'unknown')
                """,
                (self._account_id,),
            ).fetchall()
            return [int(row[0]) for row in rows]

        return await self._db(operation)

    async def _handle_group_call(self, update: types.UpdateGroupCall) -> None:
        call_id = int(update.call.id)
        existing = await self._find_by_call(call_id)
        chat_id = existing.chat_id if existing else self._chat_from_update(update)
        if chat_id is None:
            self._schedule_discovery()
            return
        previous = await self._get(chat_id)
        if isinstance(update.call, types.GroupCallDiscarded):
            await self._save(
                chat_id,
                call_id=call_id,
                access_hash=getattr(update.call, "access_hash", None),
                call_state="inactive",
                membership="left",
                transport=False,
                unavailable_reason="call_discarded",
            )
            await self._notify_once(chat_id, "call_discarded", previous)
        else:
            await self._save(
                chat_id,
                call_id=call_id,
                access_hash=getattr(update.call, "access_hash", None),
                call_state="active",
                version=getattr(update.call, "version", None),
            )

    async def _handle_participants(self, update: types.UpdateGroupCallParticipants) -> None:
        state = await self._find_by_call(int(update.call.id))
        if state is None:
            self._schedule_discovery()
            return
        if state.version is not None and update.version != state.version + 1:
            self._schedule_reconcile(state.chat_id)
            return
        own = next((item for item in update.participants if self._is_self(item)), None)
        if own is None:
            await self._save(state.chat_id, version=update.version)
            return
        await self._apply_self(state.chat_id, own, update.version, state)

    async def _fetch_self(self, chat_id: int, call: types.InputGroupCall) -> None:
        previous = await self._get(chat_id)
        try:
            result = await self._client(
                functions.phone.GetGroupParticipantsRequest(
                    call=call,
                    ids=[types.InputPeerSelf()],
                    sources=[],
                    offset="",
                    limit=1,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("self participant reconciliation failed for chat %s", chat_id, exc_info=True)
            await self._save(chat_id, call_state="unknown", membership="unknown")
            return
        own = next((item for item in result.participants if self._is_self(item)), None)
        version = getattr(result, "version", None)
        if own is not None:
            await self._apply_self(chat_id, own, version, previous)
            return
        was_joined = previous is not None and previous.membership == "joined"
        await self._save(
            chat_id,
            membership="left",
            version=version,
            transport=False if was_joined else _UNSET,
            unavailable_reason="self_removed" if was_joined else _UNSET,
        )
        if was_joined:
            await self._notify_once(chat_id, "self_removed", previous)

    async def _apply_self(
        self,
        chat_id: int,
        participant: Any,
        version: int | None,
        previous: _Activity | None,
    ) -> None:
        if bool(getattr(participant, "left", False)):
            await self._save(
                chat_id,
                membership="left",
                transport=False,
                version=version,
                unavailable_reason="self_removed",
            )
            await self._notify_once(chat_id, "self_removed", previous)
            return
        media_revoked = bool(getattr(participant, "muted", False)) and not bool(
            getattr(participant, "can_self_unmute", False)
        )
        await self._save(
            chat_id,
            membership="joined",
            version=version,
            unavailable_reason="media_revoked" if media_revoked else None,
        )
        if media_revoked:
            await self._notify_once(chat_id, "media_revoked", previous)

    async def _handle_channel_participant(self, update: Any) -> None:
        account_id = self._numeric_account_id()
        if account_id is None or int(getattr(update, "user_id", -1)) != account_id:
            return
        chat_id = get_peer_id(types.PeerChannel(int(update.channel_id)))
        participant = getattr(update, "new_participant", None)
        removed = participant is None or isinstance(participant, types.ChannelParticipantBanned)
        if not removed:
            self._schedule_reconcile(chat_id)
            return
        previous = await self._get(chat_id)
        await self._save(
            chat_id,
            membership="left",
            transport=False,
            unavailable_reason="membership_removed",
        )
        await self._notify_once(chat_id, "membership_removed", previous)

    async def _handle_chat_action(self, event: Any) -> None:
        users = await event.get_users() if hasattr(event, "get_users") else []
        account_id = self._numeric_account_id()
        if account_id is None or not any(int(user.id) == account_id for user in users):
            return
        raw_chat_id = getattr(event, "chat_id", None)
        if raw_chat_id is None:
            return
        chat_id = self._normalise_chat_id(raw_chat_id)
        removed = bool(getattr(event, "user_left", False)) or bool(
            getattr(event, "user_kicked", False)
        )
        if not removed:
            self._schedule_reconcile(chat_id)
            return
        previous = await self._get(chat_id)
        await self._save(
            chat_id,
            membership="left",
            transport=False,
            unavailable_reason="membership_removed",
        )
        await self._notify_once(chat_id, "membership_removed", previous)

    async def _notify_once(
        self, chat_id: int, reason: str, previous: _Activity | None
    ) -> None:
        if previous is not None and previous.unavailable_reason == reason:
            return
        try:
            await self._on_unavailable(chat_id, reason)
        except Exception:
            log.exception("activity unavailable callback failed for chat %s", chat_id)

    async def _discover_dialogs(self) -> None:
        semaphore = asyncio.Semaphore(self._DISCOVERY_CONCURRENCY)
        jobs: list[Awaitable[None]] = []
        try:
            async for dialog in self._client.iter_dialogs():
                if not bool(getattr(dialog, "is_group", False)):
                    continue

                async def inspect(entity: Any = dialog.entity) -> None:
                    async with semaphore:
                        try:
                            call = await self._get_active_call(entity)
                            if call is None:
                                return
                            chat_id = get_peer_id(entity)
                            input_call = self._as_input_call(call)
                            previous = await self._get(chat_id)
                            changed = previous is not None and previous.call_id not in (
                                None,
                                input_call.id,
                            )
                            await self._save(
                                chat_id,
                                call_id=input_call.id,
                                access_hash=input_call.access_hash,
                                call_state="active",
                                membership="unknown" if changed else _UNSET,
                                version=None if changed else _UNSET,
                                transport=False if changed else _UNSET,
                                unavailable_reason=None if changed else _UNSET,
                            )
                            await self._fetch_self(chat_id, input_call)
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            log.debug("could not inspect dialog group call", exc_info=True)

                jobs.append(inspect())
            if jobs:
                await asyncio.gather(*jobs)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("activity dialog discovery failed", exc_info=True)

    def _schedule_discovery(self) -> None:
        if self._stopping or not self._started:
            return
        if self._discovery_task is not None and not self._discovery_task.done():
            return
        self._discovery_task = asyncio.create_task(
            self._discover_dialogs(), name="parlay-activity-discovery"
        )

    def _schedule_reconcile(self, chat_id: int) -> None:
        if self._stopping or not self._started:
            return
        chat_id = self._normalise_chat_id(chat_id)
        existing = self._reconcile_tasks.get(chat_id)
        if existing is not None and not existing.done():
            return

        async def run() -> None:
            try:
                await self.reconcile(chat_id)
            finally:
                self._reconcile_tasks.pop(chat_id, None)

        self._reconcile_tasks[chat_id] = asyncio.create_task(
            run(), name=f"parlay-activity-{chat_id}"
        )

    async def _periodic_reconcile(self) -> None:
        while True:
            await asyncio.sleep(self._RECONCILE_SECONDS)
            for chat_id in await self._known_active_chats():
                self._schedule_reconcile(chat_id)

    async def _reconcile_safely(self, chat_id: int) -> None:
        try:
            await self.reconcile(chat_id)
        except Exception:
            log.warning("activity startup reconciliation failed", exc_info=True)

    async def _get_active_call(self, entity: Any) -> Any | None:
        if isinstance(entity, (types.Channel, types.InputPeerChannel)):
            result = await self._client(functions.channels.GetFullChannelRequest(entity))
        elif isinstance(entity, types.Chat):
            result = await self._client(functions.messages.GetFullChatRequest(entity.id))
        elif isinstance(entity, types.InputPeerChat):
            result = await self._client(
                functions.messages.GetFullChatRequest(entity.chat_id)
            )
        else:
            input_entity = await self._client.get_input_entity(entity)
            return await self._get_active_call(input_entity)
        return result.full_chat.call

    @staticmethod
    def _as_input_call(call: Any) -> types.InputGroupCall:
        if isinstance(call, types.InputGroupCall):
            return call
        return types.InputGroupCall(id=int(call.id), access_hash=int(call.access_hash))

    def _chat_from_update(self, update: Any) -> int | None:
        peer = getattr(update, "peer", None)
        if peer is not None:
            return get_peer_id(peer)
        legacy = getattr(update, "chat_id", None)
        return None if legacy is None else self._normalise_chat_id(legacy)

    @staticmethod
    def _normalise_chat_id(chat_id: int) -> int:
        value = int(chat_id)
        return value if value < 0 else get_peer_id(types.PeerChat(value))

    def _is_self(self, participant: Any) -> bool:
        if bool(getattr(participant, "is_self", False)):
            return True
        account_id = self._numeric_account_id()
        if account_id is None:
            return False
        try:
            return get_peer_id(participant.peer) == account_id
        except (TypeError, ValueError):
            return False

    def _numeric_account_id(self) -> int | None:
        try:
            return int(self._account_id)
        except ValueError:
            return None

    def _require_started(self) -> None:
        if not self._started or self._connection is None:
            raise RuntimeError("ActivityTracker is not started")


__all__ = ["ActivityTracker", "UnavailableCallback"]
