"""Durable, account-local Telegram call activity tracking.

The tracker consumes Telethon raw updates but treats them as hints. Participant
updates are partial and versioned, so gaps are reconciled with a targeted
``phone.getGroupParticipants`` request for ``InputPeerSelf``. SQLite work runs
in worker threads and never blocks the event loop used by the audio stack.
"""

from __future__ import annotations

import asyncio
import inspect
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
    """Track current voice-chat activity for one Telegram account.

    ``on_unavailable`` receives one of ``call_discarded``, ``self_removed``,
    ``media_revoked``, or ``membership_removed``. It is not called merely
    because the account is absent from a call it has never joined.
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
        """Open storage, clear stale transport flags, and reconcile Telegram."""
        async with self._lifecycle_lock:
            if self._started:
                return
            self._stopping = False
            await asyncio.to_thread(self._open_database)
            await self._execute(
                "UPDATE activity SET transport = 0, updated_at = ? WHERE account_id = ?",
                (int(time.time()), self._account_id),
            )
            self._started = True

        await self._discover_dialogs()
        for chat_id in await self._known_active_chats():
            await self._reconcile_safely(chat_id)
        if self._started and not self._stopping:
            self._periodic_task = asyncio.create_task(
                self._periodic_reconcile(), name="parlay-activity-reconcile"
            )

    async def stop(self) -> None:
        """Stop workers and close SQLite after all in-flight writes finish."""
        async with self._lifecycle_lock:
            if not self._started and self._connection is None:
                return
            self._stopping = True
            tasks = [*self._reconcile_tasks.values()]
            if self._discovery_task is not None:
                tasks.append(self._discovery_task)
            if self._periodic_task is not None:
                tasks.append(self._periodic_task)
            for task in set(tasks):
                if task is not asyncio.current_task():
                    task.cancel()
            if tasks:
                await asyncio.gather(*set(tasks), return_exceptions=True)
            self._reconcile_tasks.clear()
            self._discovery_task = None
            self._periodic_task = None
            self._started = False
            await asyncio.to_thread(self._close_database)
            self._stopping = False

    async def handle_update(self, update: Any) -> None:
        """Apply a raw Telethon update or schedule a bounded reconciliation."""
        self._require_started()
        if isinstance(update, types.UpdateGroupCall):
            await self._handle_group_call(update)
            return
        if isinstance(update, types.UpdateGroupCallParticipants):
            await self._handle_participants(update)
            return
        if isinstance(update, types.UpdateChannel):
            self._schedule_reconcile(get_peer_id(types.PeerChannel(update.channel_id)))
            return
        if type(update).__name__ == "UpdateChannelParticipant":
            await self._handle_channel_participant(update)
            return
        if any(
            bool(getattr(update, name, False))
            for name in ("user_added", "user_joined", "user_left", "user_kicked")
        ):
            await self._handle_chat_action(update)

    async def reconcile(self, chat_id: int) -> None:
        """Fetch the chat's active call and a trusted targeted self snapshot."""
        self._require_started()
        chat_id = self._normalise_chat_id(chat_id)
        try:
            entity = await self._client.get_entity(chat_id)
            active_call = await self._get_active_call(entity)
            if active_call is None:
                previous = await self._get(chat_id)
                await self._save(
                    chat_id,
                    call_state="inactive",
                    membership="left" if previous and previous.membership == "joined" else None,
                    transport=False,
                    unavailable_reason="call_discarded" if previous and previous.call_state == "active" else None,
                )
                if previous and previous.call_state == "active":
                    await self._notify_once(chat_id, "call_discarded", previous)
                return
            call = self._as_input_call(active_call)
            await self._save(
                chat_id,
                call_id=call.id,
                access_hash=call.access_hash,
                call_state="active",
                unavailable_reason=None,
            )
            await self._fetch_self(chat_id, call)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("activity reconciliation failed for chat %s", chat_id, exc_info=True)
            await self._save(chat_id, call_state="unknown", membership="unknown")

    async def set_transport(self, chat_id: int, connected: bool) -> None:
        """Record native media transport state; it never survives a restart."""
        self._require_started()
        await self._save(self._normalise_chat_id(chat_id), transport=connected)

    async def status_text(self, chat_id: int) -> str:
        """Return a concise current-state summary for command presentation."""
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
        transport = "media transport connected" if state.transport else "media transport disconnected"
        return f"{call}; {member}; {transport}."

    def _open_database(self) -> None:
        path = Path(self._db_path)
        if path.parent != Path(""):
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
        call_id: int | None | object = ..., 
        access_hash: int | None | object = ...,
        call_state: str | object = ...,
        membership: str | object = ...,
        transport: bool | object = ...,
        version: int | None | object = ...,
        unavailable_reason: str | None | object = ...,
    ) -> None:
        previous = await self._get(chat_id)
        values = {
            "call_id": previous.call_id if previous and call_id is ... else None if call_id is ... else call_id,
            "access_hash": previous.access_hash if previous and access_hash is ... else None if access_hash is ... else access_hash,
            "call_state": previous.call_state if previous and call_state is ... else "unknown" if call_state is ... else call_state,
            "membership": previous.membership if previous and membership is ... else "unknown" if membership is ... else membership,
            "transport": previous.transport if previous and transport is ... else False if transport is ... else transport,
            "version": previous.version if previous and version is ... else None if version is ... else version,
            "unavailable_reason": previous.unavailable_reason if previous and unavailable_reason is ... else None if unavailable_reason is ... else unavailable_reason,
        }
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
            (
                self._account_id,
                chat_id,
                values["call_id"],
                values["access_hash"],
                values["call_state"],
                values["membership"],
                int(bool(values["transport"])),
                values["version"],
                values["unavailable_reason"],
                int(time.time()),
            ),
        )

    async def _get(self, chat_id: int) -> _Activity | None:
        def operation(connection: sqlite3.Connection) -> _Activity | None:
            row = connection.execute(
                """
                SELECT chat_id, call_id, access_hash, call_state, membership,
                       transport, version, unavailable_reason
                FROM activity WHERE account_id = ? AND chat_id = ?
                """,
                (self._account_id, chat_id),
            ).fetchone()
            return _Activity(*row[:5], bool(row[5]), *row[6:]) if row else None

        return await self._db(operation)

    async def _find_by_call(self, call_id: int) -> _Activity | None:
        def operation(connection: sqlite3.Connection) -> _Activity | None:
            row = connection.execute(
                """
                SELECT chat_id, call_id, access_hash, call_state, membership,
                       transport, version, unavailable_reason
                FROM activity WHERE account_id = ? AND call_id = ?
                """,
                (self._account_id, call_id),
            ).fetchone()
            return _Activity(*row[:5], bool(row[5]), *row[6:]) if row else None

        return await self._db(operation)

    async def _known_active_chats(self) -> list[int]:
        def operation(connection: sqlite3.Connection) -> list[int]:
            rows = connection.execute(
                """
                SELECT chat_id FROM activity
                WHERE account_id = ? AND call_state IN ('active', 'unknown')
                """,
                (self._account_id,),
            ).fetchall()
            return [int(row[0]) for row in rows]

        return await self._db(operation)

    async def _handle_group_call(self, update: Any) -> None:
        call_id = int(update.call.id)
        existing = await self._find_by_call(call_id)
        chat_id = existing.chat_id if existing else self._chat_from_update(update)
        if chat_id is None:
            self._schedule_discovery()
            return
        if isinstance(update.call, types.GroupCallDiscarded):
            previous = await self._get(chat_id)
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
            return
        await self._save(
            chat_id,
            call_id=call_id,
            access_hash=getattr(update.call, "access_hash", None),
            call_state="active",
            version=getattr(update.call, "version", None),
            unavailable_reason=None,
        )

    async def _handle_participants(self, update: types.UpdateGroupCallParticipants) -> None:
        call_id = int(update.call.id)
        state = await self._find_by_call(call_id)
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
        if bool(getattr(own, "left", False)):
            await self._save(
                state.chat_id,
                membership="left",
                transport=False,
                version=update.version,
                unavailable_reason="self_removed",
            )
            await self._notify_once(state.chat_id, "self_removed", state)
            return
        media_revoked = bool(getattr(own, "muted", False)) and not bool(
            getattr(own, "can_self_unmute", False)
        )
        await self._save(
            state.chat_id,
            membership="joined",
            version=update.version,
            unavailable_reason="media_revoked" if media_revoked else None,
        )
        if media_revoked:
            await self._notify_once(state.chat_id, "media_revoked", state)

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
        if own is None:
            was_joined = previous is not None and previous.membership == "joined"
            await self._save(
                chat_id,
                membership="left" if was_joined else "left",
                version=version,
                unavailable_reason="self_removed" if was_joined else None,
            )
            if was_joined:
                await self._notify_once(chat_id, "self_removed", previous)
            return
        if bool(getattr(own, "left", False)):
            await self._save(
                chat_id,
                membership="left",
                transport=False,
                version=version,
                unavailable_reason="self_removed",
            )
            await self._notify_once(chat_id, "self_removed", previous)
            return
        media_revoked = bool(getattr(own, "muted", False)) and not bool(
            getattr(own, "can_self_unmute", False)
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
        if removed:
            previous = await self._get(chat_id)
            await self._save(
                chat_id,
                membership="left",
                transport=False,
                unavailable_reason="membership_removed",
            )
            await self._notify_once(chat_id, "membership_removed", previous)
        else:
            self._schedule_reconcile(chat_id)

    async def _handle_chat_action(self, event: Any) -> None:
        users = await event.get_users() if hasattr(event, "get_users") else []
        account_id = self._numeric_account_id()
        if account_id is None or not any(int(user.id) == account_id for user in users):
            return
        raw_chat_id = getattr(event, "chat_id", None)
        if raw_chat_id is None:
            return
        chat_id = self._normalise_chat_id(raw_chat_id)
        if bool(getattr(event, "user_left", False) or getattr(event, "user_kicked", False)):
            previous = await self._get(chat_id)
            await self._save(
                chat_id,
                membership="left",
                transport=False,
                unavailable_reason="membership_removed",
            )
            await self._notify_once(chat_id, "membership_removed", previous)
        else:
            self._schedule_reconcile(chat_id)

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
                entity = dialog.entity

                async def inspect_dialog(item: Any = entity) -> None:
                    async with semaphore:
                        try:
                            call = await self._get_active_call(item)
                        except Exception:
                            log.debug("could not inspect dialog for group call", exc_info=True)
                            return
                        if call is None:
                            return
                        chat_id = get_peer_id(item)
                        input_call = self._as_input_call(call)
                        await self._save(
                            chat_id,
                            call_id=input_call.id,
                            access_hash=input_call.access_hash,
                            call_state="active",
                            unavailable_reason=None,
                        )
                        await self._fetch_self(chat_id, input_call)

                jobs.append(inspect_dialog())
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

        async def run() -> None:
            await self._discover_dialogs()

        self._discovery_task = asyncio.create_task(run(), name="parlay-activity-discovery")

    def _schedule_reconcile(self, chat_id: int) -> None:
        if self._stopping or not self._started:
            return
        task = self._reconcile_tasks.get(chat_id)
        if task is not None and not task.done():
            return

        async def run() -> None:
            try:
                await self.reconcile(chat_id)
            finally:
                self._reconcile_tasks.pop(chat_id, None)

        self._reconcile_tasks[chat_id] = asyncio.create_task(
            run(), name=f"parlay-activity-chat-{chat_id}"
        )

    async def _periodic_reconcile(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._RECONCILE_SECONDS)
                for chat_id in await self._known_active_chats():
                    self._schedule_reconcile(chat_id)
        except asyncio.CancelledError:
            raise

    async def _reconcile_safely(self, chat_id: int) -> None:
        try:
            await self.reconcile(chat_id)
        except Exception:
            log.warning("activity startup reconciliation failed", exc_info=True)

    async def _get_active_call(self, entity: Any) -> Any | None:
        if isinstance(entity, types.Channel):
            full = await self._client(functions.channels.GetFullChannelRequest(entity))
        elif isinstance(entity, types.Chat):
            full = await self._client(functions.messages.GetFullChatRequest(entity.id))
        else:
            input_entity = await self._client.get_input_entity(entity)
            if isinstance(input_entity, types.InputPeerChannel):
                full = await self._client(functions.channels.GetFullChannelRequest(input_entity))
            elif isinstance(input_entity, types.InputPeerChat):
                full = await self._client(functions.messages.GetFullChatRequest(input_entity.chat_id))
            else:
                return None
        return full.full_chat.call

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
        if legacy is None:
            return None
        return self._normalise_chat_id(legacy)

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
