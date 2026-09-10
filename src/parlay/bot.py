"""Independent Telethon companion bot for Parlay rooms and Compass."""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import secrets
import sqlite3
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from telethon import Button, TelegramClient, events

Play = Callable[[int, int, str], Awaitable[dict[str, Any]]]
Authority = Callable[[int, int], Awaitable[bool] | bool]

_DURATION = re.compile(r"^(\d+)(s|m|h)$", re.IGNORECASE)
_HELP = (
    "Parlay companion commands:\n"
    "/room [duration] - create a personal room (default 2h; 5m to 24h)\n"
    "/play <query> - play in this Telegram group\n"
    "/compass on|off|reset|delete|count [1-10]|suggestions\n"
    "/start - allow private bot delivery\n"
    "/help - show this help"
)


class _BotState:
    """Small, thread-safe SQLite store for consent and opaque callbacks."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def _initialize(self) -> None:
        with self._lock, self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS bot_dm_consent(
                  user_id INTEGER PRIMARY KEY, eligible INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bot_callbacks(
                  id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, kind TEXT NOT NULL,
                  payload_json TEXT NOT NULL, result_json TEXT
                );
                """
            )

    def set_eligible(self, user_id: int, eligible: bool) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO bot_dm_consent(user_id,eligible) VALUES(?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET eligible=excluded.eligible",
                (user_id, int(eligible)),
            )

    def eligible(self, user_id: int) -> bool:
        with self._lock, self._connect() as db:
            row = db.execute(
                "SELECT eligible FROM bot_dm_consent WHERE user_id=?", (user_id,)
            ).fetchone()
            return bool(row and row["eligible"])

    def callback(self, owner_id: int, kind: str, payload: dict[str, Any]) -> str:
        token = secrets.token_urlsafe(12)
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO bot_callbacks VALUES(?,?,?,?,NULL)",
                (token, owner_id, kind, json.dumps(payload, separators=(",", ":"))),
            )
        return token

    def get_callback(self, token: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM bot_callbacks WHERE id=?", (token,)).fetchone()
            if not row:
                return None
            return {
                "owner_id": int(row["owner_id"]),
                "kind": row["kind"],
                "payload": json.loads(row["payload_json"]),
                "result": json.loads(row["result_json"]) if row["result_json"] else None,
            }

    def save_result(self, token: str, result: dict[str, Any]) -> dict[str, Any]:
        encoded = json.dumps(result, separators=(",", ":"))
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT result_json FROM bot_callbacks WHERE id=?", (token,)
            ).fetchone()
            if not row:
                raise KeyError(token)
            if row["result_json"]:
                return json.loads(row["result_json"])
            db.execute("UPDATE bot_callbacks SET result_json=? WHERE id=?", (encoded, token))
            return result


class CompanionBot:
    """A separately authenticated bot that exposes room and Compass entry points."""

    def __init__(
        self,
        config: Any,
        rooms: Any,
        compass: Any,
        play: Play,
        authority: Authority | None = None,
        *,
        client: Any | None = None,
    ) -> None:
        self.config = config
        self.rooms = rooms
        self.compass = compass
        self.play = play
        self.authority = authority
        state_path = getattr(config, "bot_db_path", "data/parlay-bot.sqlite3")
        self._state = _BotState(state_path)
        self._client = client
        self.username = self._clean_username(getattr(config, "bot_username", None))
        self._activation_locks: dict[str, asyncio.Lock] = {}
        self._registered = False

    @property
    def client(self) -> Any | None:
        """Expose the client so the parent can monitor connection state."""
        return self._client

    async def start(self) -> None:
        token = str(getattr(self.config, "bot_token", "") or "").strip()
        if not token:
            raise RuntimeError("Companion bot is configured but bot_token is missing")
        mini_app_url = str(getattr(self.config, "mini_app_url", "") or "").strip()
        if not mini_app_url.startswith("https://"):
            raise RuntimeError("mini_app_url must be an HTTPS URL")
        if self._client is None:
            self._client = TelegramClient(
                getattr(self.config, "bot_session", "data/parlay-bot"),
                int(self.config.api_id),
                str(self.config.api_hash),
            )
        self._register_handlers()
        await self._client.start(bot_token=token)
        if not self.username:
            me = await self._client.get_me()
            self.username = self._clean_username(getattr(me, "username", None))
        if not self.username:
            raise RuntimeError("The companion bot needs a Telegram username")

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.disconnect()

    def _register_handlers(self) -> None:
        assert self._client is not None
        if self._registered:
            return
        self._client.add_event_handler(
            self._on_message, events.NewMessage(incoming=True, forwards=False)
        )
        self._client.add_event_handler(self._on_callback, events.CallbackQuery())
        self._client.add_event_handler(self._on_inline, events.InlineQuery())
        self._registered = True

    async def _on_message(self, event: Any) -> None:
        text = (getattr(event, "raw_text", "") or "").strip()
        if not text.startswith("/"):
            return
        head, _, tail = text.partition(" ")
        command = head.split("@", 1)[0].lower()
        args = tail.strip()
        handlers = {
            "/start": self._command_start,
            "/help": self._command_help,
            "/room": self._command_room,
            "/play": self._command_play,
            "/compass": self._command_compass,
        }
        handler = handlers.get(command)
        if handler:
            try:
                await handler(event, args)
            except Exception as error:
                await event.reply(self._safe_error(error))

    async def _verified_sender(self, event: Any) -> Any | None:
        sender = await event.get_sender()
        sender_id = getattr(sender, "id", None)
        if (
            not isinstance(sender_id, int)
            or isinstance(sender_id, bool)
            or bool(getattr(sender, "bot", False))
            or sender.__class__.__name__ in {"Channel", "Chat"}
        ):
            return None
        return sender

    async def _command_start(self, event: Any, args: str) -> None:
        sender = await self._verified_sender(event)
        if sender is None or not bool(getattr(event, "is_private", False)):
            return
        await asyncio.to_thread(self._state.set_eligible, sender.id, True)
        await event.reply("Private delivery is available. Compass stays off until /compass on.")

    async def _command_help(self, event: Any, args: str) -> None:
        await event.reply(_HELP)

    async def _command_room(self, event: Any, args: str) -> None:
        sender = await self._verified_sender(event)
        if sender is None:
            return
        parts = args.split()
        if len(parts) > 1:
            await event.reply("Usage: /room [5m-24h]")
            return
        try:
            duration = self.parse_duration(parts[0] if parts else None)
        except ValueError as error:
            await event.reply(str(error))
            return
        invited_id = None
        if bool(getattr(event, "is_reply", False)):
            reply = await event.get_reply_message()
            replied_sender = await reply.get_sender() if reply else None
            replied_id = getattr(replied_sender, "id", None)
            if (
                not isinstance(replied_id, int)
                or isinstance(replied_id, bool)
                or bool(getattr(replied_sender, "bot", False))
                or replied_sender.__class__.__name__ in {"Channel", "Chat"}
            ):
                await event.reply("Reply to a human Telegram user to invite them.")
                return
            invited_id = replied_id
        room = await self.rooms.create_personal(sender.id, invited_id, duration)
        await event.reply(
            f"Room ready for {self._duration_text(duration)}.",
            buttons=Button.url("Open room", self.room_url(str(room["id"]))),
        )

    async def _command_play(self, event: Any, args: str) -> None:
        sender = await self._verified_sender(event)
        chat_id = getattr(event, "chat_id", None)
        if sender is None:
            return
        if not bool(getattr(event, "is_group", False)) or not isinstance(chat_id, int):
            await event.reply("/play is available only in Telegram groups.")
            return
        if not args:
            await event.reply("Usage: /play <search or supported URL>")
            return
        allowed = await self._authorized(sender.id, chat_id)
        if not allowed:
            await event.reply("You are not authorized to control playback in this group.")
            return
        room = await self.play(sender.id, chat_id, args)
        await event.reply(
            "Playback updated.",
            buttons=Button.url("Open room", self.room_url(str(room["id"]))),
        )

    async def _authorized(self, user_id: int, chat_id: int) -> bool:
        if self.authority is not None:
            result = self.authority(user_id, chat_id)
            return bool(await result) if inspect.isawaitable(result) else bool(result)
        operator = str(getattr(self.config, "operator_id", "") or "").strip()
        return operator == str(user_id)

    async def _command_compass(self, event: Any, args: str) -> None:
        sender = await self._verified_sender(event)
        if sender is None:
            return
        parts = args.lower().split()
        if not parts:
            await event.reply("Usage: /compass on|off|reset|delete|count [1-10]|suggestions")
            return
        user_id = str(sender.id)
        command = parts[0]
        if command in {"on", "off"} and len(parts) == 1:
            if command == "on" and not bool(getattr(event, "is_private", False)):
                await event.reply("Send /compass on in a private chat with the bot.")
                return
            if command == "on" and not await asyncio.to_thread(self._state.eligible, sender.id):
                await event.reply("Send /start in this private chat first.")
                return
            await asyncio.to_thread(self.compass.set_preferences, user_id, command == "on")
            await event.reply(f"Compass is {command}.")
        elif command == "count" and len(parts) == 2 and parts[1].isdigit():
            count = int(parts[1])
            if not 1 <= count <= 10:
                await event.reply("Compass count must be from 1 to 10.")
                return
            await asyncio.to_thread(self.compass.set_preferences, user_id, True, count)
            await event.reply(f"Compass will send {count} recommendation(s).")
        elif command == "reset" and len(parts) == 1:
            await asyncio.to_thread(self.compass.reset_user, user_id)
            await event.reply("Compass history controls were reset.")
        elif command == "delete" and len(parts) == 1:
            await asyncio.to_thread(self.compass.delete_user, user_id)
            await event.reply("Compass data was deleted.")
        elif command == "suggestions" and len(parts) == 1:
            items = await asyncio.to_thread(self.compass.recommend, user_id)
            if not items:
                await event.reply("No Compass suggestions are available.")
            else:
                await self._send_items(sender.id, "Compass suggestions", items, event.reply)
        else:
            await event.reply("Usage: /compass on|off|reset|delete|count [1-10]|suggestions")

    async def _on_inline(self, event: Any) -> None:
        sender_id = int(getattr(event, "sender_id", 0) or 0)
        if sender_id <= 0 or (getattr(event, "text", "") or "").strip().lower() != "room":
            await event.answer([], cache_time=0)
            return
        token = await asyncio.to_thread(
            self._state.callback, sender_id, "activate_room", {"duration": 7200}
        )
        result = event.builder.article(
            "Create a Parlay room",
            text="Parlay room preview. Only the initiating user can confirm creation.",
            description="Confirm to create one personal room",
            buttons=Button.inline("Confirm room", self._callback_data(token)),
        )
        await event.answer([result], cache_time=0, private=True)

    async def _on_callback(self, event: Any) -> None:
        data = bytes(getattr(event, "data", b"") or b"")
        if not data.startswith(b"p:"):
            return
        token = data[2:].decode("ascii", "ignore")
        record = await asyncio.to_thread(self._state.get_callback, token)
        if not record:
            await event.answer("This action expired.", alert=True)
            return
        sender_id = int(getattr(event, "sender_id", 0) or 0)
        if sender_id != record["owner_id"]:
            await event.answer("This action belongs to another user.", alert=True)
            return
        try:
            if record["kind"] == "activate_room":
                await self._activate_inline(event, token, record)
            elif record["kind"] == "compass_feedback":
                await self._feedback(event, token, record)
            elif record["kind"] == "reentry":
                await self._approve_reentry(event, token, record)
            else:
                await event.answer("Unsupported action.", alert=True)
        except Exception as error:
            await event.answer(self._safe_error(error), alert=True)

    async def _activate_inline(self, event: Any, token: str, record: dict[str, Any]) -> None:
        lock = self._activation_locks.setdefault(token, asyncio.Lock())
        async with lock:
            current = await asyncio.to_thread(self._state.get_callback, token)
            result = current["result"] if current else None
            if result is None:
                room = await self.rooms.create_personal(
                    record["owner_id"], None, int(record["payload"]["duration"])
                )
                result = await asyncio.to_thread(
                    self._state.save_result, token, {"room_id": str(room["id"])}
                )
        assert result is not None
        await event.edit(
            "Parlay room ready.",
            buttons=Button.url("Open room", self.room_url(result["room_id"])),
        )
        await event.answer()

    async def _feedback(self, event: Any, token: str, record: dict[str, Any]) -> None:
        if record["result"] is None:
            payload = record["payload"]
            accepted = await asyncio.to_thread(
                self.compass.feedback,
                str(record["owner_id"]),
                str(payload["track_id"]),
                bool(payload["positive"]),
                f"bot:{token}",
            )
            await asyncio.to_thread(self._state.save_result, token, {"accepted": bool(accepted)})
        await event.answer("Feedback saved.")

    async def _approve_reentry(self, event: Any, token: str, record: dict[str, Any]) -> None:
        result = record["result"]
        payload = record["payload"]
        if result is None:
            snapshot = await self.rooms.action(
                str(payload["room_id"]),
                record["owner_id"],
                f"bot-reentry:{token}",
                int(payload["expected_revision"]),
                "approve_reentry",
                {"user_id": int(payload["user_id"])},
            )
            result = await asyncio.to_thread(
                self._state.save_result, token, {"room_id": str(snapshot["id"])}
            )
        await event.edit(
            "Re-entry approved.",
            buttons=Button.url("Open room", self.room_url(result["room_id"])),
        )
        await event.answer()

    async def notify_reentry(self, owner_id: int, room_id: str, user_id: int) -> bool:
        """Ask the room owner to approve re-entry using the current owner snapshot revision."""
        if not await asyncio.to_thread(self._state.eligible, owner_id):
            return False
        try:
            assert self._client is not None
            snapshot = await self.rooms.snapshot(room_id, owner_id)
            token = await asyncio.to_thread(
                self._state.callback,
                owner_id,
                "reentry",
                {
                    "room_id": room_id,
                    "user_id": user_id,
                    "expected_revision": int(snapshot["revision"]),
                },
            )
            await self._client.send_message(
                owner_id,
                f"User {user_id} requests re-entry.",
                buttons=Button.inline("Approve re-entry", self._callback_data(token)),
            )
            return True
        except Exception:
            return False

    async def deliver(self, user_id: str | int, text: str, items: list[dict[str, Any]]) -> bool:
        """Deliver Compass picks. Return False if private delivery is unavailable."""
        uid = int(user_id)
        if not await asyncio.to_thread(self._state.eligible, uid):
            return False
        try:
            assert self._client is not None
            await self._send_items(uid, text, items, self._client.send_message)
            return True
        except Exception:
            await asyncio.to_thread(self._state.set_eligible, uid, False)
            return False

    async def _send_items(
        self,
        user_id: int,
        text: str,
        items: list[dict[str, Any]],
        send: Callable[..., Awaitable[Any]],
    ) -> None:
        lines = [text]
        rows = []
        for index, item in enumerate(items[:10], 1):
            title = str(item.get("title") or "Untitled")
            lines.append(f"{index}. {title}")
            minus = await asyncio.to_thread(
                self._state.callback,
                user_id,
                "compass_feedback",
                {"track_id": str(item["id"]), "positive": False},
            )
            plus = await asyncio.to_thread(
                self._state.callback,
                user_id,
                "compass_feedback",
                {"track_id": str(item["id"]), "positive": True},
            )
            rows.append(
                [
                    Button.inline(f"- {index}", self._callback_data(minus)),
                    Button.inline(f"+ {index}", self._callback_data(plus)),
                ]
            )
        await send("\n".join(lines), buttons=rows)

    def room_url(self, room_id: str) -> str:
        if self.username:
            return f"https://t.me/{quote(self.username)}?startapp={quote(room_id)}"
        return str(getattr(self.config, "mini_app_url", "")).rstrip("/") + "?room=" + quote(room_id)

    @staticmethod
    def parse_duration(value: str | None) -> int:
        if value is None:
            return 7200
        match = _DURATION.fullmatch(value.strip())
        if not match:
            raise ValueError(
                "Duration must be one value in seconds, minutes, or hours, such as 30m."
            )
        amount = int(match.group(1))
        seconds = amount * {"s": 1, "m": 60, "h": 3600}[match.group(2).lower()]
        if not 300 <= seconds <= 86400:
            raise ValueError("Duration must be from 5 minutes to 24 hours.")
        return seconds

    @staticmethod
    def _duration_text(seconds: int) -> str:
        if seconds % 3600 == 0:
            return f"{seconds // 3600} hour(s)"
        if seconds % 60 == 0:
            return f"{seconds // 60} minute(s)"
        return f"{seconds} seconds"

    @staticmethod
    def _callback_data(token: str) -> bytes:
        data = f"p:{token}".encode("ascii")
        if len(data) > 64:
            raise ValueError("Callback payload exceeds Telegram's 64-byte limit")
        return data

    @staticmethod
    def _clean_username(value: Any) -> str | None:
        text = str(value or "").strip().lstrip("@")
        return text or None

    @staticmethod
    def _safe_error(error: Exception) -> str:
        message = str(error).strip()
        if error.__class__.__name__ in {"RoomError", "ValueError"} and message:
            return message[:200]
        return "The action could not be completed. Please try again."
