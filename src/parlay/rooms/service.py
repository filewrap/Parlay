"""SQLite-backed authoritative listening-room state."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import logging
import math
import os
import sqlite3
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

Playback = Callable[[int, str, dict[str, Any]], Awaitable[dict[str, Any] | None]]
Check = Callable[[int, int], Awaitable[bool] | bool]
Reentry = Callable[[int, str, int], Awaitable[object] | object]
Search = Callable[[str], Awaitable[list[dict[str, Any]]]]
ActionEvent = Callable[[str, int, str, dict[str, Any], str], Awaitable[object]]

log = logging.getLogger(__name__)


class RoomError(RuntimeError):
    """A safe API error raised by the room authority."""

    def __init__(self, code: str, message: str, status: int = 400, snapshot: dict | None = None):
        super().__init__(message)
        self.code, self.status, self.snapshot = code, status, snapshot


class RoomService:
    """Persist room state and serialize mutations by room."""

    def __init__(
        self,
        db_path: str | Path,
        playback: Playback | None = None,
        *,
        authority: Check | None = None,
        member: Check | None = None,
        on_reentry: Reentry | None = None,
        search: Search | None = None,
        on_action: ActionEvent | None = None,
    ) -> None:
        self.db_path = str(db_path)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.playback = playback
        self.authority = authority
        self.member = member
        self.on_reentry = on_reentry
        self.search = search
        self.on_action = on_action
        self._locks: dict[str, asyncio.Lock] = {}
        self._listeners: dict[str, set[asyncio.Queue]] = {}
        self._expiry_task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def _initialize(self) -> None:
        with self._connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS rooms(
              id TEXT PRIMARY KEY, kind TEXT NOT NULL, owner_id INTEGER NOT NULL,
              chat_id INTEGER, call_id INTEGER, created_at REAL NOT NULL,
              expires_at REAL, revision INTEGER NOT NULL, state TEXT NOT NULL,
              end_reason TEXT, data_json TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS active_group_room
              ON rooms(chat_id) WHERE kind='group' AND state!='ended';
            CREATE TABLE IF NOT EXISTS room_actions(
              room_id TEXT NOT NULL, user_id INTEGER NOT NULL, action_id TEXT NOT NULL,
              digest TEXT NOT NULL, snapshot_json TEXT NOT NULL, created_at REAL NOT NULL,
              PRIMARY KEY(room_id,user_id,action_id),
              FOREIGN KEY(room_id) REFERENCES rooms(id) ON DELETE CASCADE
            );
            """)

    async def start(self) -> None:
        await asyncio.to_thread(self._expire_due)
        if not self._expiry_task or self._expiry_task.done():
            self._stopping.clear()
            self._expiry_task = asyncio.create_task(self._expiry_loop(), name="room-expiry")

    async def stop(self) -> None:
        self._stopping.set()
        if self._expiry_task:
            self._expiry_task.cancel()
            try:
                await self._expiry_task
            except asyncio.CancelledError:
                pass
            self._expiry_task = None

    async def create_personal(
        self, owner_id: int, invited_id: int | None = None, duration: int = 7200
    ) -> dict:
        if not isinstance(owner_id, int) or isinstance(owner_id, bool):
            raise RoomError("invalid_owner", "owner_id must be an integer")
        if isinstance(duration, bool) or not 300 <= duration <= 86400:
            raise RoomError("invalid_duration", "duration must be from 300 to 86400 seconds")
        now, room_id = time.time(), uuid.uuid4().hex
        data = self._new_data(owner_id)
        if invited_id is not None:
            data["invited"] = [int(invited_id)]
        row = (
            room_id,
            "personal",
            owner_id,
            None,
            None,
            now,
            now + duration,
            1,
            "active",
            None,
            json.dumps(data, separators=(",", ":")),
        )
        await asyncio.to_thread(self._insert_room, row)
        return await self.snapshot(room_id, owner_id)

    async def ensure_group(self, chat_id: int, call_id: int, owner_id: int) -> dict:
        """Internal bot integration only. The HTTP gateway never exposes this operation."""

        def ensure() -> str:
            with self._connect() as db:
                found = db.execute(
                    "SELECT id,call_id FROM rooms WHERE chat_id=? AND kind='group' AND state!='ended'",
                    (chat_id,),
                ).fetchone()
                if found and found["call_id"] == call_id:
                    return str(found["id"])
                if found:
                    db.execute(
                        "UPDATE rooms SET state='ended',end_reason='call_replaced',revision=revision+1 WHERE id=?",
                        (found["id"],),
                    )
                room_id = uuid.uuid4().hex
                data = self._new_data(owner_id)
                data["settings"]["capacity"] = 15
                db.execute(
                    "INSERT INTO rooms VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        room_id,
                        "group",
                        owner_id,
                        chat_id,
                        call_id,
                        time.time(),
                        None,
                        1,
                        "active",
                        None,
                        json.dumps(data, separators=(",", ":")),
                    ),
                )
                return room_id

        room_id = await asyncio.to_thread(ensure)
        result = await self.snapshot(room_id, owner_id)
        self._publish(room_id, result)
        return result

    async def end_group(self, chat_id: int, reason: str) -> None:
        def end() -> list[str]:
            with self._connect() as db:
                rows = db.execute(
                    "SELECT id FROM rooms WHERE chat_id=? AND kind='group' AND state!='ended'",
                    (chat_id,),
                ).fetchall()
                db.execute(
                    "UPDATE rooms SET state='ended',end_reason=?,revision=revision+1 WHERE chat_id=? AND kind='group' AND state!='ended'",
                    (str(reason)[:200], chat_id),
                )
                return [str(row["id"]) for row in rows]

        for room_id in await asyncio.to_thread(end):
            row = await asyncio.to_thread(self._load, room_id)
            self._publish(room_id, self._snapshot(row, None))

    async def set_recovering(self, chat_id: int, reason: str) -> None:
        """Mark an active group room as recovering after a transient media failure."""
        room_id = await asyncio.to_thread(self._group_id, chat_id)
        if not room_id:
            return
        async with self._lock(room_id):
            row = await asyncio.to_thread(self._load, room_id)
            self._active(row)
            reason = str(reason)[:200]
            if row["state"] == "recovering" and row["end_reason"] == reason:
                return
            revision = row["revision"] + 1
            await asyncio.to_thread(
                self._save_status, room_id, revision, self._data(row), "recovering", reason
            )
            output = await self._snapshot_for(
                self._replace(row, revision, self._data(row), "recovering"), row["owner_id"]
            )
        self._publish(room_id, output)

    async def publish_playback(self, chat_id: int, snapshot: dict) -> None:
        """Publish an actual registry snapshot and mark a recovering group active."""
        room_id = await asyncio.to_thread(self._group_id, chat_id)
        if not room_id:
            return
        actual = self._clean_playback(snapshot)
        async with self._lock(room_id):
            row = await asyncio.to_thread(self._load, room_id)
            self._active(row)
            data = self._data(row)
            duplicate = self._playback_anchor(data["playback"]) == self._playback_anchor(actual)
            if duplicate and row["state"] == "active":
                return
            data["playback"] = actual
            revision = row["revision"] + 1
            await asyncio.to_thread(self._save_status, room_id, revision, data, "active", None)
            output = await self._snapshot_for(
                self._replace(row, revision, data, "active"), row["owner_id"]
            )
        self._publish(room_id, output)

    async def join(self, room_id: str, user: dict, password: str = "") -> dict:
        user_id = self._user_id(user)
        async with self._lock(room_id):
            row = await asyncio.to_thread(self._load, room_id)
            self._active(row)
            data = self._data(row)
            if row["kind"] == "group" and not await self._check(
                self.member, user_id, row["chat_id"]
            ):
                raise RoomError("not_group_member", "Telegram group membership is required", 403)
            members = data["members"]
            existing = next((m for m in members if m["user_id"] == user_id), None)
            if user_id in data["kicked"]:
                raise RoomError("reentry_required", "The owner must approve re-entry", 403)
            if not existing:
                if (
                    row["kind"] == "personal"
                    and data.get("invited")
                    and user_id not in data["invited"]
                ):
                    raise RoomError("not_invited", "This personal room is invite-only", 403)
                if len(members) >= data["settings"]["capacity"]:
                    raise RoomError("room_full", "The room is at capacity", 409)
                if data["password_hash"] and not self._verify_password(
                    password, data["password_hash"]
                ):
                    raise RoomError("wrong_password", "The room password is incorrect", 403)
                members.append(self._member(user, "participant"))
            else:
                existing.update(
                    {k: v for k, v in self._member(user, existing["role"]).items() if v is not None}
                )
            revision = row["revision"] + (0 if existing else 1)
            if not existing:
                await asyncio.to_thread(self._save, room_id, revision, data)
            output = self._snapshot(self._replace(row, revision, data), user_id)
        if not existing:
            self._publish(room_id, output)
        return output

    async def snapshot(self, room_id: str, user_id: int) -> dict:
        row = await asyncio.to_thread(self._load, room_id)
        self._active(row)
        data = self._data(row)
        if not any(m["user_id"] == user_id for m in data["members"]):
            raise RoomError("not_admitted", "Join the room before accessing it", 403)
        if user_id in data["kicked"]:
            raise RoomError("access_revoked", "Room access was revoked", 403)
        if row["kind"] == "group" and not await self._check(self.member, user_id, row["chat_id"]):
            raise RoomError("not_group_member", "Telegram group membership is required", 403)
        return await self._snapshot_for(row, user_id)

    async def action(
        self,
        room_id: str,
        user_id: int,
        action_id: str,
        expected_revision: int,
        action: str,
        payload: dict,
    ) -> dict:
        if not isinstance(action_id, str) or not 1 <= len(action_id) <= 128:
            raise RoomError("invalid_action_id", "action_id is required")
        if not isinstance(payload, dict):
            raise RoomError("invalid_payload", "payload must be an object")
        digest = hashlib.sha256(
            json.dumps(
                [expected_revision, action, payload], sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        notify: tuple[int, str, int] | None = None
        async with self._lock(room_id):
            row = await asyncio.to_thread(self._load, room_id)
            self._active(row)
            data = self._data(row)
            member = next((m for m in data["members"] if m["user_id"] == user_id), None)
            reentry_request = (
                action == "request_reentry" and user_id in data["kicked"] and not member
            )
            if row["kind"] == "group" and not await self._check(
                self.member, user_id, row["chat_id"]
            ):
                raise RoomError("not_group_member", "Telegram group membership is required", 403)
            if not member and reentry_request:
                member = {"user_id": user_id, "role": "participant"}
            if not member or (user_id in data["kicked"] and not reentry_request):
                raise RoomError("access_revoked", "Room access is not active", 403)

            replay = await asyncio.to_thread(self._replay, room_id, user_id, action_id)
            if replay:
                if not hmac.compare_digest(replay["digest"], digest):
                    raise RoomError(
                        "action_id_reused", "action_id was already used for another request", 409
                    )
                return json.loads(replay["snapshot_json"])
            if row["revision"] != expected_revision and not reentry_request:
                raise RoomError(
                    "stale_revision",
                    "Room state changed. Refresh and retry.",
                    409,
                    await self._snapshot_for(row, user_id),
                )

            resolved = None
            if action in {"queue_add", "force_play"}:
                query = payload.get("query")
                if not isinstance(query, str) or not query.strip() or len(query) > 500:
                    raise RoomError("invalid_query", "query must be a non-empty string")
                if not self.search:
                    raise RoomError("search_unsupported", "Media search is not configured", 501)
                tracks = await self.search(query.strip())
                if not tracks:
                    raise RoomError("track_not_found", "No playable track was found", 404)
                resolved = self._clean_track(tracks[0])

            changed, notify, playback_call = await self._apply(
                row, data, member, action, payload, resolved
            )
            if playback_call:
                if not self.playback:
                    raise RoomError("playback_unavailable", "Group playback is not configured", 503)
                actual = await self.playback(*playback_call)
                if not actual:
                    raise RoomError(
                        "playback_unavailable", "Group playback did not confirm the action", 503
                    )
                data["playback"] = self._clean_playback(actual)

            revision = row["revision"] + (1 if changed else 0)
            state = "active" if playback_call else data.get("force_state", row["state"])
            new_row = self._replace(row, revision, data, state)
            if reentry_request:
                output = {"status": "pending"}
                publication = await self._snapshot_for(new_row, row["owner_id"])
            else:
                output = await self._snapshot_for(new_row, user_id)
                publication = output
            await asyncio.to_thread(
                self._commit_action,
                room_id,
                revision,
                data,
                user_id,
                action_id,
                digest,
                output,
                changed,
                state,
            )
        if notify and self.on_reentry:
            reentry_result = self.on_reentry(*notify)
            if inspect.isawaitable(reentry_result):
                await reentry_result
        self._publish(room_id, publication)
        if resolved is not None and action in {"queue_add", "force_play"} and self.on_action:
            event_id = f"{room_id}:{user_id}:{action_id}"
            try:
                await self.on_action(room_id, user_id, action, self._action_track(resolved), event_id)
            except Exception:
                log.exception("room action callback failed", extra={"event_id": event_id})
        return output

    async def _apply(self, row, data, actor, action, payload, resolved):
        uid, role = actor["user_id"], actor["role"]
        personal_owner = row["kind"] == "personal" and uid == row["owner_id"]
        moderator = role == "moderator"
        settings = data["settings"]
        authority = personal_owner
        if row["kind"] == "group":
            authority = await self._check(self.authority, uid, row["chat_id"])
        privileged = authority if row["kind"] == "group" else personal_owner
        notify = playback_call = None
        if action == "settings":
            if not privileged:
                raise RoomError("owner_lock", "Only the room authority can change settings", 403)
            allowed = {
                "capacity",
                "password",
                "owner_lock",
                "queue_all",
                "theme",
                "tv_size",
                "duration",
            }
            if set(payload) - allowed:
                raise RoomError("invalid_settings", "Unknown setting")
            if "capacity" in payload:
                cap = payload["capacity"]
                if isinstance(cap, bool) or not isinstance(cap, int) or not 2 <= cap <= 15:
                    raise RoomError("invalid_capacity", "capacity must be from 2 to 15")
                if cap < len(data["members"]):
                    raise RoomError(
                        "capacity_below_members", "capacity is below current membership"
                    )
                settings["capacity"] = cap
            if "password" in payload:
                if row["kind"] != "personal":
                    raise RoomError("unsupported_setting", "Group rooms do not use room passwords")
                password = payload["password"]
                if not isinstance(password, str) or len(password) > 128:
                    raise RoomError("invalid_password", "password must be at most 128 characters")
                data["password_hash"] = self._hash_password(password) if password else None
            for key in ("owner_lock", "queue_all"):
                if key in payload:
                    if not isinstance(payload[key], bool):
                        raise RoomError("invalid_settings", f"{key} must be boolean")
                    settings[key] = payload[key]
            for key in ("theme", "tv_size"):
                if key in payload:
                    if not isinstance(payload[key], str) or len(payload[key]) > 64:
                        raise RoomError("invalid_settings", f"{key} must be a short string")
                    settings[key] = payload[key]
            if "duration" in payload:
                if row["kind"] != "personal":
                    raise RoomError("unsupported_setting", "Group rooms do not expire by duration")
                duration = payload["duration"]
                if (
                    isinstance(duration, bool)
                    or not isinstance(duration, int)
                    or not 300 <= duration <= 86400
                ):
                    raise RoomError(
                        "invalid_duration", "duration must be from 300 to 86400 seconds"
                    )
                data["expires_override"] = row["created_at"] + duration
        elif action in {"queue_add", "force_play"}:
            if not (privileged or moderator or settings["queue_all"]):
                raise RoomError("queue_forbidden", "Queue changes are restricted", 403)
            if settings["owner_lock"] and not personal_owner and row["kind"] == "personal":
                raise RoomError("owner_lock", "Owner Lock is enabled", 403)
            if row["kind"] == "group":
                playback_call = (row["chat_id"], action, {"track": resolved})
            elif action == "queue_add" and data["playback"]["track"]:
                data["playback"]["queue"].append(resolved)
            else:
                data["playback"].update(
                    {
                        "track": resolved,
                        "status": "playing",
                        "position_seconds": 0.0,
                        "server_time": time.time(),
                    }
                )
        elif action in {"pause", "resume", "skip"}:
            if not (
                privileged or moderator or (settings["queue_all"] and not settings["owner_lock"])
            ):
                raise RoomError("control_forbidden", "Playback controls are restricted", 403)
            if row["kind"] == "group":
                playback_call = (row["chat_id"], action, {})
            else:
                pb = self._timeline(data["playback"])
                if action == "pause":
                    pb["status"] = "paused"
                elif action == "resume" and pb["track"]:
                    pb["status"] = "playing"
                    pb["server_time"] = time.time()
                else:
                    pb["track"] = pb["queue"].pop(0) if pb["queue"] else None
                    pb["status"] = "playing" if pb["track"] else "idle"
                    pb["position_seconds"], pb["server_time"] = 0.0, time.time()
                data["playback"] = pb
        elif action == "kick":
            target = self._target(payload)
            target_member = next(
                (item for item in data["members"] if item["user_id"] == target), None
            )
            target_authority = row["kind"] == "group" and await self._check(
                self.authority, target, row["chat_id"]
            )
            protected = (
                moderator
                and not privileged
                and target_member is not None
                and (target_member["role"] in {"owner", "moderator"} or target_authority)
            )
            if (
                not (privileged or moderator)
                or (row["kind"] == "personal" and target == row["owner_id"])
                or protected
            ):
                raise RoomError("moderation_forbidden", "This member cannot be removed", 403)
            data["members"] = [m for m in data["members"] if m["user_id"] != target]
            if target not in data["kicked"]:
                data["kicked"].append(target)
            data["pending_reentry"] = [x for x in data["pending_reentry"] if x != target]
        elif action == "moderator":
            if not personal_owner:
                raise RoomError(
                    "moderation_forbidden", "Only the personal room owner can delegate", 403
                )
            target = self._target(payload)
            found = next((m for m in data["members"] if m["user_id"] == target), None)
            if not found or target == row["owner_id"]:
                raise RoomError("member_not_found", "Member was not found", 404)
            found["role"] = "moderator" if bool(payload.get("enabled")) else "participant"
        elif action == "request_reentry":
            if uid not in data["kicked"]:
                raise RoomError("reentry_not_required", "Re-entry approval is not required")
            if uid not in data["pending_reentry"]:
                data["pending_reentry"].append(uid)
                notify = (row["owner_id"], row["id"], uid)
        elif action == "approve_reentry":
            if not privileged:
                raise RoomError(
                    "owner_required", "Only the room authority can approve re-entry", 403
                )
            target = self._target(payload)
            if target not in data["pending_reentry"]:
                raise RoomError("request_not_found", "No pending re-entry request", 404)
            data["pending_reentry"].remove(target)
            data["kicked"] = [x for x in data["kicked"] if x != target]
            if target not in data["invited"]:
                data["invited"].append(target)
        elif action == "leave":
            if personal_owner:
                data["force_state"] = "ended"
            else:
                data["members"] = [m for m in data["members"] if m["user_id"] != uid]
        elif action == "close":
            if not privileged:
                code = "group_authority_required" if row["kind"] == "group" else "owner_required"
                raise RoomError(code, "Room authority is required", 403)
            data["force_state"] = "ended"
        elif action == "appearance":
            for key in ("avatar", "outfit"):
                if key in payload:
                    if not isinstance(payload[key], str) or len(payload[key]) > 64:
                        raise RoomError("invalid_appearance", f"{key} must be a short string")
                    actor[key] = payload[key]
        else:
            raise RoomError("unknown_action", "Unsupported room action")
        return True, notify, playback_call

    def subscribe(self, room_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=32)
        self._listeners.setdefault(room_id, set()).add(queue)
        return queue

    def unsubscribe(self, room_id: str, queue: asyncio.Queue) -> None:
        self._listeners.get(room_id, set()).discard(queue)

    def _publish(self, room_id: str, snapshot: dict) -> None:
        for queue in tuple(self._listeners.get(room_id, ())):
            try:
                queue.put_nowait({"type": "snapshot", "snapshot": snapshot})
            except asyncio.QueueFull:
                try:
                    queue.put_nowait({"type": "disconnect"})
                except asyncio.QueueFull:
                    pass

    async def _snapshot_for(self, row, user_id: int | None) -> dict:
        group_authority = False
        if row["kind"] == "group" and user_id is not None:
            group_authority = await self._check(self.authority, user_id, row["chat_id"])
        return self._snapshot(row, user_id, group_authority)

    def _snapshot(self, row, user_id: int | None, group_authority: bool = False) -> dict:
        data, pb = self._data(row), self._timeline(self._data(row)["playback"])
        role = next((m["role"] for m in data["members"] if m["user_id"] == user_id), None)
        personal_owner = row["kind"] == "personal" and user_id == row["owner_id"]
        authority = group_authority if row["kind"] == "group" else personal_owner
        settings = dict(data["settings"])
        settings["password_required"] = bool(data["password_hash"])
        expires = data.get("expires_override", row["expires_at"])
        state = data.get("force_state", row["state"])
        can_shared = authority or role == "moderator"
        if row["kind"] == "personal" and settings["owner_lock"] and not personal_owner:
            can_shared = False
        return {
            "id": row["id"],
            "kind": row["kind"],
            "owner_id": row["owner_id"],
            "chat_id": row["chat_id"],
            "revision": row["revision"],
            "state": state,
            "expires_at": expires,
            "settings": settings,
            "members": [{k: v for k, v in m.items() if v is not None} for m in data["members"]],
            "pending_reentry": list(data["pending_reentry"]) if authority else [],
            "playback": pb,
            "permissions": {
                "manage_settings": authority,
                "queue": can_shared or settings["queue_all"],
                "control": can_shared or (settings["queue_all"] and not settings["owner_lock"]),
                "moderate": can_shared,
                "close": authority,
            },
        }

    @staticmethod
    def _new_data(owner_id: int) -> dict:
        return {
            "settings": {
                "capacity": 2,
                "owner_lock": True,
                "queue_all": False,
                "theme": "default",
                "tv_size": "medium",
            },
            "password_hash": None,
            "invited": [],
            "kicked": [],
            "pending_reentry": [],
            "members": [{"user_id": owner_id, "first_name": str(owner_id), "role": "owner"}],
            "playback": {
                "track": None,
                "status": "idle",
                "position_seconds": 0.0,
                "server_time": time.time(),
                "queue": [],
            },
        }

@staticmethod
def _action_track(track: dict[str, Any]) -> dict[str, Any]:
candidate = track.get("youtube_id")
valid = isinstance(candidate, str) and len(candidate) == 11 and all(
    char.isalnum() or char in "-_" for char in candidate
)
if not valid:
    parsed = urlsplit(track["source_url"])
    candidate = (
        parsed.path.strip("/").split("/", 1)[0]
        if parsed.netloc.lower() in {"youtu.be", "www.youtu.be"}
        else parse_qs(parsed.query).get("v", [""])[0]
    )
if not isinstance(candidate, str) or len(candidate) != 11 or not all(
    char.isalnum() or char in "-_" for char in candidate
):
    raise RoomError("invalid_track", "Search returned an invalid YouTube track")
output = dict(track)
output["youtube_id"] = candidate
return output
    @staticmethod
    def _clean_track(track: dict) -> dict:
        if (
            not isinstance(track, dict)
            or not isinstance(track.get("id"), str)
            or not isinstance(track.get("title"), str)
        ):
            raise RoomError("invalid_track", "Search returned an invalid track")
        url = track.get("source_url", "")
        if not isinstance(url, str) or not (
            url.startswith("https://www.youtube.com/") or url.startswith("https://youtu.be/")
        ):
            raise RoomError("unsupported_source", "Only HTTPS YouTube page URLs are supported")
        output = {"id": track["id"][:200], "title": track["title"][:500], "source_url": url[:2000]}
        if isinstance(track.get("youtube_id"), str):
            output["youtube_id"] = track["youtube_id"][:32]
        if isinstance(track.get("duration"), (int, float)) and math.isfinite(track["duration"]):
            output["duration"] = max(0.0, float(track["duration"]))
        return output

    def _clean_playback(self, value: dict) -> dict:
        if not isinstance(value, dict):
            raise RoomError("invalid_playback", "playback snapshot must be an object")
        track = self._clean_track(value["track"]) if value.get("track") else None
        queue = [self._clean_track(x) for x in value.get("queue", [])[:100]]
        status = value.get("status", "idle")
        if status not in {"idle", "playing", "paused"}:
            status = "idle"
        return {
            "track": track,
            "status": status,
            "position_seconds": max(0.0, float(value.get("position_seconds", 0))),
            "server_time": time.time(),
            "queue": queue,
        }

    @staticmethod
    def _timeline(pb: dict) -> dict:
        result = json.loads(json.dumps(pb))
        now = time.time()
        if result["status"] == "playing" and result["track"]:
            result["position_seconds"] += max(0.0, now - result["server_time"])
            duration = result["track"].get("duration")
            while duration is not None and result["position_seconds"] >= duration:
                result["position_seconds"] -= duration
                result["track"] = result["queue"].pop(0) if result["queue"] else None
                if not result["track"]:
                    result["status"], result["position_seconds"] = "idle", 0.0
                    break
                duration = result["track"].get("duration")
        result["server_time"] = now
        return result

    @staticmethod
    def _hash_password(password: str) -> str:
        salt = os.urandom(16)
        digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
        return f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"

    @staticmethod
    def _verify_password(password: str, encoded: str) -> bool:
        try:
            _, n, r, p, salt, expected = encoded.split("$")
            actual = hashlib.scrypt(
                password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p)
            )
            return hmac.compare_digest(actual, bytes.fromhex(expected))
        except (ValueError, TypeError):
            return False

    async def _check(self, callback, user_id, chat_id) -> bool:
        if callback is None:
            return False
        result = callback(user_id, chat_id)
        return bool(await result) if inspect.isawaitable(result) else bool(result)

    @staticmethod
    def _user_id(user):
        value = user.get("id") if isinstance(user, dict) else None
        if isinstance(value, bool) or not isinstance(value, int):
            raise RoomError("invalid_user", "Telegram user id is required")
        return value

    @staticmethod
    def _member(user, role):
        first = user.get("first_name")
        if not isinstance(first, str) or not first:
            first = str(user["id"])
        result = {"user_id": user["id"], "first_name": first[:128], "role": role}
        if isinstance(user.get("photo_url"), str):
            result["photo_url"] = user["photo_url"][:2000]
        return result

    @staticmethod
    def _target(payload):
        value = payload.get("user_id")
        if isinstance(value, bool) or not isinstance(value, int):
            raise RoomError("invalid_user", "user_id must be an integer")
        return value

    def _active(self, row):
        data = self._data(row)
        expires = data.get("expires_override", row["expires_at"])
        if data.get("force_state", row["state"]) == "ended":
            raise RoomError("room_ended", "The room has ended", 410)
        if expires is not None and expires <= time.time():
            with self._connect() as db:
                db.execute(
                    "UPDATE rooms SET state='ended',end_reason='expired',revision=revision+1 WHERE id=?",
                    (row["id"],),
                )
            raise RoomError("room_expired", "The room has expired", 410)

    def _insert_room(self, row):
        with self._connect() as db:
            db.execute("INSERT INTO rooms VALUES(?,?,?,?,?,?,?,?,?,?,?)", row)

    def _load(self, room_id):
        with self._connect() as db:
            row = db.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
        if not row:
            raise RoomError("room_not_found", "Room was not found", 404)
        return row

    def _group_id(self, chat_id):
        with self._connect() as db:
            row = db.execute(
                "SELECT id FROM rooms WHERE chat_id=? AND kind='group' AND state!='ended'",
                (chat_id,),
            ).fetchone()
        return str(row["id"]) if row else None

    def _save(self, room_id, revision, data):
        state = data.get("force_state")
        with self._connect() as db:
            db.execute(
                "UPDATE rooms SET revision=?,data_json=?,state=COALESCE(?,state) WHERE id=?",
                (revision, json.dumps(data, separators=(",", ":")), state, room_id),
            )

    def _replay(self, room_id, user_id, action_id):
        with self._connect() as db:
            return db.execute(
                "SELECT digest,snapshot_json FROM room_actions WHERE room_id=? AND user_id=? AND action_id=?",
                (room_id, user_id, action_id),
            ).fetchone()

    def _commit_action(
        self, room_id, revision, data, user_id, action_id, digest, output, changed, state
    ):
        with self._connect() as db:
            if changed:
                db.execute(
                    "UPDATE rooms SET revision=?,data_json=?,state=?,end_reason=? WHERE id=?",
                    (
                        revision,
                        json.dumps(data, separators=(",", ":")),
                        state,
                        None if state == "active" else data.get("end_reason"),
                        room_id,
                    ),
                )
            db.execute(
                "INSERT INTO room_actions VALUES(?,?,?,?,?,?)",
                (
                    room_id,
                    user_id,
                    action_id,
                    digest,
                    json.dumps(output, separators=(",", ":")),
                    time.time(),
                ),
            )

    def _save_status(self, room_id, revision, data, state, reason):
        with self._connect() as db:
            db.execute(
                "UPDATE rooms SET revision=?,data_json=?,state=?,end_reason=? WHERE id=?",
                (revision, json.dumps(data, separators=(",", ":")), state, reason, room_id),
            )

    @staticmethod
    def _playback_anchor(playback):
        return {
            "track": playback.get("track"),
            "status": playback.get("status"),
            "position_seconds": playback.get("position_seconds", 0),
            "queue": playback.get("queue", []),
        }

    def _expire_due(self):
        now = time.time()
        with self._connect() as db:
            rows = db.execute(
                "SELECT id,expires_at,data_json FROM rooms WHERE kind='personal' AND state!='ended'"
            ).fetchall()
            expired = []
            for row in rows:
                data = json.loads(row["data_json"])
                expires_at = data.get("expires_override", row["expires_at"])
                if expires_at is not None and expires_at <= now:
                    expired.append((row["id"],))
            db.executemany(
                "UPDATE rooms SET state='ended',end_reason='expired',"
                "revision=revision+1 WHERE id=? AND state!='ended'",
                expired,
            )

    async def _expiry_loop(self):
        while not self._stopping.is_set():
            await asyncio.to_thread(self._expire_due)
            try:
                await asyncio.wait_for(self._stopping.wait(), 15)
            except TimeoutError:
                pass

    def _lock(self, room_id):
        return self._locks.setdefault(room_id, asyncio.Lock())

    @staticmethod
    def _data(row):
        return json.loads(row["data_json"])

    @staticmethod
    def _replace(row, revision, data, state=None):
        result = dict(row)
        result["revision"] = revision
        result["data_json"] = json.dumps(data)
        if state is not None:
            result["state"] = state
        elif data.get("force_state"):
            result["state"] = data["force_state"]
        return result
