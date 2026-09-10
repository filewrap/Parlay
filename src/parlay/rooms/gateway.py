"""FastAPI HTTP and WebSocket gateway for authoritative rooms."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import math
import secrets
import sqlite3
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar, cast
from urllib.parse import parse_qsl, urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .service import RoomError, RoomService

AUTH_MAX_AGE = 300
SESSION_TTL = 900
TICKET_TTL = 30
MAX_BODY_BYTES = 65_536
T = TypeVar("T")
Search = Callable[[str], Awaitable[list[dict[str, Any]]]]
Identity = tuple[str, dict[str, Any], float]


def validate_init_data(init_data: str, bot_token: str, now: float | None = None) -> dict[str, Any]:
    """Validate Telegram Mini App initData with the official WebAppData key derivation."""
    if not bot_token:
        raise ValueError("bot_token must not be empty")
    if not isinstance(init_data, str) or len(init_data.encode()) > 16_384:
        raise ValueError("invalid initData")
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError as error:
        raise ValueError("invalid initData") from error
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate initData field")
    values = dict(pairs)
    supplied = values.pop("hash", None)
    values.pop("signature", None)
    if supplied is None or len(supplied) != 64:
        raise ValueError("missing initData hash")
    data_check = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(supplied.lower(), expected):
        raise ValueError("invalid initData signature")
    try:
        auth_date = int(values["auth_date"])
    except (KeyError, ValueError) as error:
        raise ValueError("invalid auth_date") from error
    current = time.time() if now is None else now
    if auth_date < current - AUTH_MAX_AGE:
        raise ValueError("initData expired")
    if auth_date > current + 30:
        raise ValueError("initData is from the future")
    try:
        raw_user = json.loads(values["user"])
    except (KeyError, json.JSONDecodeError) as error:
        raise ValueError("invalid Telegram user") from error
    if not isinstance(raw_user, dict):
        raise ValueError("invalid Telegram user")
    user_id = raw_user.get("id")
    if isinstance(user_id, bool) or not isinstance(user_id, int):
        raise ValueError("invalid Telegram user")
    user: dict[str, Any] = {
        "id": user_id,
        "first_name": str(raw_user.get("first_name") or user_id)[:128],
    }
    photo_url = raw_user.get("photo_url")
    if isinstance(photo_url, str):
        user["photo_url"] = photo_url[:2000]
    return {"user": user, "start_param": values.get("start_param")}


class InitBody(BaseModel):
    init_data: str = Field(max_length=16_384)


class JoinBody(BaseModel):
    password: str = Field(default="", max_length=128)


class ActionBody(BaseModel):
    action_id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=0)
    action: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)


class TicketBody(BaseModel):
    room_id: str = Field(min_length=1, max_length=128)


class PreferencesBody(BaseModel):
    enabled: bool
    count: int = Field(default=10, ge=1, le=10)
    quiet_start: int | None = Field(default=None, ge=0, le=23)
    quiet_end: int | None = Field(default=None, ge=0, le=23)
    timezone: str = Field(default="UTC", max_length=128)


class FeedbackBody(BaseModel):
    track_id: str = Field(min_length=1, max_length=200)
    positive: bool
    event_id: str = Field(min_length=1, max_length=200)


class BodyLimitMiddleware:
    """Reject declared and streamed bodies above the configured limit."""

    def __init__(self, app: ASGIApp, limit: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            await self._reject(send)
            return
        if declared > self.limit:
            await self._reject(send)
            return
        consumed = 0

        async def limited_receive() -> Message:
            nonlocal consumed
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > self.limit:
                    raise BodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except BodyTooLarge:
            await self._reject(send)

    @staticmethod
    async def _reject(send: Send) -> None:
        body = b'{"error":{"code":"body_too_large","message":"Request body is too large"}}'
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


class BodyTooLarge(Exception):
    """Internal body-limit signal."""


@dataclass(eq=False)
class SocketConnection:
    websocket: WebSocket
    room_id: str
    user_id: int
    expires_at: float
    outbound: asyncio.Queue[dict[str, Any] | None] = field(
        default_factory=lambda: asyncio.Queue(maxsize=32)
    )

    def enqueue(self, message: dict[str, Any]) -> bool:
        try:
            self.outbound.put_nowait(message)
        except asyncio.QueueFull:
            return False
        return True


class GatewayState:
    def __init__(
        self,
        service: RoomService,
        origins: list[str],
        compass: Any,
        search: Search | None,
    ) -> None:
        self.service = service
        self.origins = set(origins)
        self.compass = compass
        self.search = search
        self.db_path = service.db_path
        self.limits: dict[str, deque[float]] = defaultdict(deque)
        self.presence: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
        self.sockets: dict[str, set[SocketConnection]] = defaultdict(set)
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS room_sessions(
                    token_hash TEXT PRIMARY KEY,
                    user_json TEXT NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS room_ws_tickets(
                    ticket_hash TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL,
                    room_id TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    used_at REAL
                );
                """
            )

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def rate(self, key: str, maximum: int, window: int) -> None:
        now = time.monotonic()
        bucket = self.limits[key]
        while bucket and bucket[0] <= now - window:
            bucket.popleft()
        if len(bucket) >= maximum:
            raise HTTPException(429, "Too many requests")
        bucket.append(now)

    def session(self, token: str) -> Identity:
        if not token or len(token) > 256:
            raise HTTPException(401, "Session expired")
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.connect() as db:
            row = db.execute(
                "SELECT user_json,expires_at FROM room_sessions WHERE token_hash=?", (digest,)
            ).fetchone()
        if row is None or float(row["expires_at"]) <= time.time():
            raise HTTPException(401, "Session expired")
        user = json.loads(row["user_json"])
        if not isinstance(user, dict):
            raise HTTPException(401, "Session expired")
        return digest, user, float(row["expires_at"])

    def consume_ticket(self, ticket: str, room_id: str) -> tuple[dict[str, Any], float] | None:
        if not ticket or len(ticket) > 256:
            return None
        now = time.time()
        digest = hashlib.sha256(ticket.encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT t.room_id,t.expires_at,t.used_at,s.user_json,
                          s.expires_at AS session_expiry
                   FROM room_ws_tickets t
                   JOIN room_sessions s ON s.token_hash=t.token_hash
                   WHERE t.ticket_hash=?""",
                (digest,),
            ).fetchone()
            valid = bool(
                row
                and row["room_id"] == room_id
                and row["used_at"] is None
                and float(row["expires_at"]) > now
                and float(row["session_expiry"]) > now
            )
            if not valid:
                db.rollback()
                return None
            db.execute("UPDATE room_ws_tickets SET used_at=? WHERE ticket_hash=?", (now, digest))
            db.commit()
        assert row is not None
        user = json.loads(row["user_json"])
        return cast(dict[str, Any], user), float(row["session_expiry"])

    def enqueue_room(self, room_id: str, message: dict[str, Any]) -> None:
        for connection in tuple(self.sockets.get(room_id, ())):
            if not connection.enqueue(message):
                connection.enqueue(None)  # type: ignore[arg-type]


def _validated_origins(values: list[str]) -> list[str]:
    if not values:
        raise ValueError("allowed_origins must contain explicit HTTP(S) origins")
    output: list[str] = []
    for value in values:
        parsed = urlsplit(value)
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("allowed_origins must be origins without paths")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and local):
            raise ValueError("allowed_origins must use HTTPS except for loopback development")
        output.append(f"{parsed.scheme}://{parsed.netloc}")
    return output


def create_app(
    service: RoomService,
    bot_token: str,
    allowed_origins: list[str],
    compass: Any = None,
    search: Search | None = None,
) -> FastAPI:
    """Create a gateway. The parent remains responsible for service start and stop."""
    if not bot_token:
        raise ValueError("bot_token must not be empty")
    origins = _validated_origins(allowed_origins)
    if search is not None:
        service.search = search
    state = GatewayState(service, origins, compass, search)
    app = FastAPI(title="Parlay Rooms API")
    app.add_middleware(BodyLimitMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.exception_handler(RoomError)
    async def room_error(_request: Request, error: RoomError) -> JSONResponse:
        body: dict[str, Any] = {"error": {"code": error.code, "message": str(error)}}
        if error.snapshot is not None:
            body["error"]["snapshot"] = error.snapshot
        return JSONResponse(body, status_code=error.status)

    async def identity(authorization: str = Header(default="")) -> Identity:
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, "Bearer token required")
        return state.session(authorization[7:])

    @app.post("/api/auth")
    async def auth(body: InitBody, request: Request) -> dict[str, Any]:
        host = request.client.host if request.client else "unknown"
        state.rate(f"auth-ip:{host}", 20, 60)
        try:
            result = validate_init_data(body.init_data, bot_token)
        except ValueError as error:
            raise HTTPException(401, str(error)) from error
        user = cast(dict[str, Any], result["user"])
        state.rate(f"auth-user:{user['id']}", 10, 60)
        token = secrets.token_urlsafe(32)
        expiry = time.time() + SESSION_TTL
        with state.connect() as db:
            db.execute("DELETE FROM room_sessions WHERE expires_at<=?", (time.time(),))
            db.execute(
                "INSERT INTO room_sessions VALUES(?,?,?)",
                (hashlib.sha256(token.encode()).hexdigest(), json.dumps(user), expiry),
            )
        return {
            "token": token,
            "expires_at": expiry,
            "user": user,
            "start_param": result["start_param"],
        }

    @app.post("/api/rooms/{room_id}/join")
    async def join(
        room_id: str,
        body: JoinBody,
        request: Request,
        auth_data: Identity = Depends(identity),
    ) -> dict[str, Any]:
        user = auth_data[1]
        host = request.client.host if request.client else "unknown"
        state.rate(f"join-ip:{host}:{room_id}", 30, 60)
        state.rate(f"join-user:{user['id']}:{room_id}", 10, 60)
        return await service.join(room_id, user, body.password)

    @app.get("/api/rooms/{room_id}")
    async def snapshot(room_id: str, auth_data: Identity = Depends(identity)) -> dict[str, Any]:
        return await service.snapshot(room_id, int(auth_data[1]["id"]))

    @app.post("/api/rooms/{room_id}/actions")
    async def action(
        room_id: str,
        body: ActionBody,
        auth_data: Identity = Depends(identity),
    ) -> dict[str, Any]:
        return await service.action(
            room_id,
            int(auth_data[1]["id"]),
            body.action_id,
            body.expected_revision,
            body.action,
            body.payload,
        )

    @app.get("/api/search")
    async def media_search(
        q: str = Query(min_length=1, max_length=500),
        auth_data: Identity = Depends(identity),
    ) -> dict[str, Any]:
        state.rate(f"search:{auth_data[1]['id']}", 30, 60)
        if search is None:
            raise HTTPException(501, "Media search is not configured")
        tracks = await search(q)
        return {"tracks": [service._clean_track(track) for track in tracks[:25]]}

    @app.post("/api/ws-ticket")
    async def ticket(body: TicketBody, auth_data: Identity = Depends(identity)) -> dict[str, str]:
        token_hash, user, expiry = auth_data
        await service.snapshot(body.room_id, int(user["id"]))
        value = secrets.token_urlsafe(32)
        now = time.time()
        with state.connect() as db:
            db.execute(
                "DELETE FROM room_ws_tickets WHERE expires_at<=? OR used_at IS NOT NULL", (now,)
            )
            db.execute(
                "INSERT INTO room_ws_tickets VALUES(?,?,?,?,NULL)",
                (
                    hashlib.sha256(value.encode()).hexdigest(),
                    token_hash,
                    body.room_id,
                    min(now + TICKET_TTL, expiry),
                ),
            )
        return {"ticket": value}

    async def compass_call(name: str, *args: Any) -> Any:
        if compass is None:
            raise HTTPException(501, "Compass is not configured")
        method = getattr(compass, name, None)
        if method is None or not callable(method):
            raise HTTPException(501, "Compass operation is not configured")
        if inspect.iscoroutinefunction(method):
            return await method(*args)
        return await asyncio.to_thread(method, *args)

    @app.get("/api/compass")
    async def recommendations(auth_data: Identity = Depends(identity)) -> dict[str, Any]:
        user_id = str(auth_data[1]["id"])
        return {"recommendations": await compass_call("recommend", user_id)}

    @app.post("/api/compass/preferences")
    async def preferences(
        body: PreferencesBody, auth_data: Identity = Depends(identity)
    ) -> dict[str, bool]:
        await compass_call(
            "set_preferences",
            str(auth_data[1]["id"]),
            body.enabled,
            body.count,
            body.quiet_start,
            body.quiet_end,
            body.timezone,
        )
        return {"ok": True}

    @app.post("/api/compass/feedback")
    async def feedback(
        body: FeedbackBody, auth_data: Identity = Depends(identity)
    ) -> dict[str, bool]:
        user_id = str(auth_data[1]["id"])
        accepted = await compass_call(
            "feedback", user_id, body.track_id, body.positive, f"{user_id}:{body.event_id}"
        )
        return {"accepted": bool(accepted)}

    @app.post("/api/compass/reset")
    async def reset(auth_data: Identity = Depends(identity)) -> dict[str, bool]:
        await compass_call("reset_user", str(auth_data[1]["id"]))
        return {"ok": True}

    @app.post("/api/compass/delete")
    @app.delete("/api/compass")
    async def delete(auth_data: Identity = Depends(identity)) -> dict[str, bool]:
        await compass_call("delete_user", str(auth_data[1]["id"]))
        return {"ok": True}

    @app.websocket("/api/rooms/{room_id}/ws")
    async def websocket_endpoint(
        websocket: WebSocket, room_id: str, ticket_value: str = Query(alias="ticket")
    ) -> None:
        if websocket.headers.get("origin") not in state.origins:
            await websocket.close(1008, "Origin not allowed")
            return
        consumed = state.consume_ticket(ticket_value, room_id)
        if consumed is None:
            await websocket.close(1008, "Invalid ticket")
            return
        user, session_expiry = consumed
        user_id = int(user["id"])
        try:
            initial = await service.snapshot(room_id, user_id)
        except RoomError:
            await websocket.close(1008, "Room access denied")
            return
        await websocket.accept()
        connection = SocketConnection(websocket, room_id, user_id, session_expiry)
        state.sockets[room_id].add(connection)
        state.presence[room_id][user_id] = {
            "user_id": user_id,
            "x": 0.0,
            "z": 0.0,
            "rotation": 0.0,
            "at": time.monotonic(),
            "seq": -1,
        }
        service_queue = service.subscribe(room_id)
        connection.enqueue({"type": "snapshot", "snapshot": initial})
        _queue_presence(state, room_id)

        async def send_messages() -> None:
            while True:
                message = await connection.outbound.get()
                if message is None:
                    await websocket.close(1013, "Slow client")
                    return
                await asyncio.wait_for(websocket.send_json(message), 2.0)

        async def receive_messages() -> None:
            while True:
                message = await websocket.receive_json()
                if not isinstance(message, dict) or message.get("type") != "move":
                    continue
                _accept_movement(state, room_id, user_id, message)

        async def monitor_access() -> None:
            while True:
                try:
                    await asyncio.wait_for(service_queue.get(), 0.25)
                except TimeoutError:
                    pass
                if time.time() >= session_expiry:
                    await websocket.close(1008, "Session expired")
                    return
                try:
                    current = await service.snapshot(room_id, user_id)
                except RoomError:
                    await websocket.close(1008, "Room access revoked")
                    return
                if not connection.enqueue({"type": "snapshot", "snapshot": current}):
                    await websocket.close(1013, "Slow client")
                    return

        tasks: list[asyncio.Task[None]] = [
            asyncio.create_task(send_messages()),
            asyncio.create_task(receive_messages()),
            asyncio.create_task(monitor_access()),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if not task.cancelled():
                    task.exception()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            service.unsubscribe(room_id, service_queue)
            state.sockets[room_id].discard(connection)
            if not any(item.user_id == user_id for item in state.sockets[room_id]):
                state.presence[room_id].pop(user_id, None)
            _queue_presence(state, room_id)

    app.state.rooms = state
    return app


def _accept_movement(
    state: GatewayState, room_id: str, user_id: int, message: dict[str, Any]
) -> None:
    current = state.presence[room_id].get(user_id)
    if current is None:
        return
    now = time.monotonic()
    if now - float(current["at"]) < 0.095:
        return
    values = [message.get(key) for key in ("x", "z", "rotation")]
    if not all(
        isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        for value in values
    ):
        return
    x, z, rotation = (float(cast(int | float, value)) for value in values)
    if abs(x) > 1_000 or abs(z) > 1_000 or abs(rotation) > math.tau * 4:
        return
    sequence = message.get("seq")
    if sequence is not None and (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence <= int(current["seq"])
    ):
        return
    elapsed = max(now - float(current["at"]), 0.1)
    if math.hypot(x - float(current["x"]), z - float(current["z"])) / elapsed > 25:
        return
    current.update(
        {
            "x": x,
            "z": z,
            "rotation": rotation,
            "at": now,
            "seq": sequence if sequence is not None else current["seq"],
        }
    )
    _queue_presence(state, room_id)


def _queue_presence(state: GatewayState, room_id: str) -> None:
    players = [
        {key: value for key, value in item.items() if key in {"user_id", "x", "z", "rotation"}}
        for item in state.presence[room_id].values()
    ]
    state.enqueue_room(room_id, {"type": "presence", "players": players})
