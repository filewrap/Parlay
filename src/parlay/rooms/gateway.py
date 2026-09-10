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
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qsl

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .service import RoomError, RoomService

AUTH_MAX_AGE = 300
SESSION_TTL = 900
TICKET_TTL = 30


def validate_init_data(init_data: str, bot_token: str, now: float | None = None) -> dict:
    """Validate Telegram Mini App initData using the official WebAppData derivation."""
    if not bot_token:
        raise ValueError("bot_token must not be empty")
    if not isinstance(init_data, str) or len(init_data.encode()) > 16_384:
        raise ValueError("invalid initData")
    pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    values = dict(pairs)
    supplied = values.pop("hash", None)
    values.pop("signature", None)
    if not supplied or len(supplied) != 64:
        raise ValueError("missing initData hash")
    check = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
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
        user = json.loads(values["user"])
    except (KeyError, json.JSONDecodeError) as error:
        raise ValueError("invalid Telegram user") from error
    if isinstance(user.get("id"), bool) or not isinstance(user.get("id"), int):
        raise ValueError("invalid Telegram user")
    clean = {"id": user["id"], "first_name": str(user.get("first_name") or user["id"])[:128]}
    if isinstance(user.get("photo_url"), str): clean["photo_url"] = user["photo_url"][:2000]
    return {"user": clean, "start_param": values.get("start_param")}


class InitBody(BaseModel): init_data: str = Field(max_length=16_384)
class JoinBody(BaseModel): password: str = Field(default="", max_length=128)
class ActionBody(BaseModel):
    action_id: str = Field(min_length=1, max_length=128)
    expected_revision: int = Field(ge=0)
    action: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)
class TicketBody(BaseModel): room_id: str = Field(min_length=1, max_length=128)
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


class GatewayState:
    def __init__(self, service, bot_token, origins, compass, search):
        self.service, self.bot_token = service, bot_token
        self.origins, self.compass, self.search = set(origins), compass, search
        self.db_path = service.db_path
        self.limits: dict[str, deque] = defaultdict(deque)
        self.presence: dict[str, dict[int, dict]] = defaultdict(dict)
        self.sockets: dict[tuple[str, int], set[WebSocket]] = defaultdict(set)
        with sqlite3.connect(self.db_path) as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS room_sessions(token_hash TEXT PRIMARY KEY,user_json TEXT NOT NULL,expires_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS room_ws_tickets(ticket_hash TEXT PRIMARY KEY,token_hash TEXT NOT NULL,room_id TEXT NOT NULL,expires_at REAL NOT NULL,used_at REAL);
            """)

    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=30); db.row_factory = sqlite3.Row
        return db
    def rate(self, key: str, maximum: int, window: int):
        now, bucket = time.monotonic(), self.limits[key]
        while bucket and bucket[0] <= now-window: bucket.popleft()
        if len(bucket) >= maximum: raise HTTPException(429, "Too many requests")
        bucket.append(now)
    def session(self, token: str):
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.connect() as db: row = db.execute("SELECT * FROM room_sessions WHERE token_hash=?", (digest,)).fetchone()
        if not row or row["expires_at"] <= time.time(): raise HTTPException(401, "Session expired")
        return digest, json.loads(row["user_json"]), row["expires_at"]
    def admitted(self, room_id, user_id):
        try:
            with self.connect() as db: row = db.execute("SELECT data_json,state,expires_at FROM rooms WHERE id=?", (room_id,)).fetchone()
            if not row or row["state"] == "ended" or (row["expires_at"] and row["expires_at"] <= time.time()): return False
            data = json.loads(row["data_json"])
            return user_id not in data["kicked"] and any(m["user_id"] == user_id for m in data["members"])
        except (sqlite3.Error, KeyError, json.JSONDecodeError): return False


def create_app(
    service: RoomService, bot_token: str, allowed_origins: list[str], compass: Any = None,
    search: Callable | None = None,
) -> FastAPI:
    if not bot_token:
        raise ValueError("bot_token must not be empty")
    if not allowed_origins or any(not x.startswith(("https://", "http://localhost", "http://127.0.0.1")) for x in allowed_origins):
        raise ValueError("allowed_origins must contain explicit HTTP(S) origins")
    service.search = search
    state = GatewayState(service, bot_token, allowed_origins, compass, search)
    app = FastAPI(title="Parlay Rooms API")
    app.add_middleware(CORSMiddleware, allow_origins=allowed_origins, allow_credentials=False,
                       allow_methods=["GET", "POST", "DELETE"], allow_headers=["Authorization", "Content-Type"])

    @app.middleware("http")
    async def body_limit(request: Request, call_next):
        length = request.headers.get("content-length")
        if length and int(length) > 65_536: return _error_response(413, "body_too_large", "Request body is too large")
        return await call_next(request)

    @app.exception_handler(RoomError)
    async def room_error(_request, error: RoomError):
        from fastapi.responses import JSONResponse
        body = {"error": {"code": error.code, "message": str(error)}}
        if error.snapshot is not None: body["error"]["snapshot"] = error.snapshot
        return JSONResponse(body, status_code=error.status)

    async def identity(authorization: str = Header(default="")):
        if not authorization.startswith("Bearer "): raise HTTPException(401, "Bearer token required")
        return state.session(authorization[7:])

    @app.post("/api/auth")
    async def auth(body: InitBody, request: Request):
        state.rate(f"auth-ip:{request.client.host if request.client else 'unknown'}", 20, 60)
        try: result = validate_init_data(body.init_data, bot_token)
        except ValueError as error: raise HTTPException(401, str(error)) from error
        state.rate(f"auth-user:{result['user']['id']}", 10, 60)
        token, expiry = secrets.token_urlsafe(32), time.time()+SESSION_TTL
        with state.connect() as db:
            db.execute("DELETE FROM room_sessions WHERE expires_at<=?", (time.time(),))
            db.execute("INSERT INTO room_sessions VALUES(?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), json.dumps(result["user"]), expiry))
        return {"token": token, "expires_at": expiry, "user": result["user"], "start_param": result["start_param"]}

    @app.post("/api/rooms/{room_id}/join")
    async def join(room_id: str, body: JoinBody, request: Request, auth=Depends(identity)):
        _, user, _ = auth
        ip = request.client.host if request.client else "unknown"
        state.rate(f"join-ip:{ip}:{room_id}", 30, 60); state.rate(f"join-user:{user['id']}:{room_id}", 10, 60)
        return await service.join(room_id, user, body.password)

    @app.get("/api/rooms/{room_id}")
    async def snapshot(room_id: str, auth=Depends(identity)): return await service.snapshot(room_id, auth[1]["id"])

    @app.post("/api/rooms/{room_id}/actions")
    async def action(room_id: str, body: ActionBody, auth=Depends(identity)):
        return await service.action(room_id, auth[1]["id"], body.action_id, body.expected_revision, body.action, body.payload)

    @app.get("/api/search")
    async def media_search(q: str = Query(min_length=1, max_length=500), auth=Depends(identity)):
        state.rate(f"search:{auth[1]['id']}", 30, 60)
        if not search: raise HTTPException(501, "Media search is not configured")
        tracks = await search(q)
        return {"tracks": [service._clean_track(track) for track in tracks[:25]]}

    @app.post("/api/ws-ticket")
    async def ticket(body: TicketBody, auth=Depends(identity)):
        token_hash, user, expiry = auth
        await service.snapshot(body.room_id, user["id"])
        value = secrets.token_urlsafe(32)
        with state.connect() as db:
            db.execute("DELETE FROM room_ws_tickets WHERE expires_at<=? OR used_at IS NOT NULL", (time.time(),))
            db.execute("INSERT INTO room_ws_tickets VALUES(?,?,?,?,NULL)", (hashlib.sha256(value.encode()).hexdigest(), token_hash, body.room_id, min(time.time()+TICKET_TTL, expiry)))
        return {"ticket": value}

    async def compass_call(name, *args):
        if compass is None: raise HTTPException(501, "Compass is not configured")
        method = getattr(compass, name)
        if inspect.iscoroutinefunction(method): return await method(*args)
        return await asyncio.to_thread(method, *args)

    @app.get("/api/compass")
    async def recommendations(auth=Depends(identity)):
        return {"recommendations": await compass_call("recommend", str(auth[1]["id"]))}
    @app.post("/api/compass/preferences")
    async def preferences(body: PreferencesBody, auth=Depends(identity)):
        await compass_call("set_preferences", str(auth[1]["id"]), body.enabled, body.count, body.quiet_start, body.quiet_end, body.timezone)
        return {"ok": True}
    @app.post("/api/compass/feedback")
    async def feedback(body: FeedbackBody, auth=Depends(identity)):
        return {"accepted": await compass_call("feedback", str(auth[1]["id"]), body.track_id, body.positive, body.event_id)}
    @app.post("/api/compass/reset")
    async def reset(auth=Depends(identity)):
        await compass_call("reset_user", str(auth[1]["id"])); return {"ok": True}
    @app.post("/api/compass/delete")
    @app.delete("/api/compass")
    async def delete(auth=Depends(identity)):
        await compass_call("delete_user", str(auth[1]["id"])); return {"ok": True}

    @app.websocket("/api/rooms/{room_id}/ws")
    async def websocket(websocket: WebSocket, room_id: str, ticket: str = Query()):
        origin = websocket.headers.get("origin")
        if origin not in state.origins: await websocket.close(1008, "Origin not allowed"); return
        digest = hashlib.sha256(ticket.encode()).hexdigest()
        with state.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT t.*,s.user_json,s.expires_at session_expiry FROM room_ws_tickets t JOIN room_sessions s ON s.token_hash=t.token_hash WHERE t.ticket_hash=?", (digest,)).fetchone()
            if not row or row["room_id"] != room_id or row["used_at"] is not None or row["expires_at"] <= time.time() or row["session_expiry"] <= time.time():
                db.rollback(); await websocket.close(1008, "Invalid ticket"); return
            db.execute("UPDATE room_ws_tickets SET used_at=? WHERE ticket_hash=?", (time.time(),digest)); db.commit()
        user, session_expiry = json.loads(row["user_json"]), row["session_expiry"]
        try: initial = await service.snapshot(room_id, user["id"])
        except RoomError: await websocket.close(1008, "Room access denied"); return
        await websocket.accept()
        queue = service.subscribe(room_id); key = (room_id,user["id"]); state.sockets[key].add(websocket)
        state.presence[room_id][user["id"]] = {"user_id":user["id"],"x":0.0,"z":0.0,"rotation":0.0,"at":time.monotonic(),"seq":-1}
        await websocket.send_json({"type":"snapshot","snapshot":initial})
        await _broadcast_presence(state, room_id)
        async def sender():
            while True:
                try: event = await asyncio.wait_for(queue.get(), 1.0)
                except TimeoutError:
                    if time.time() >= session_expiry or not state.admitted(room_id,user["id"]): raise WebSocketDisconnect(1008)
                    continue
                if event.get("type") == "disconnect": raise WebSocketDisconnect(1013)
                await asyncio.wait_for(websocket.send_json(event), 2.0)
        async def receiver():
            while True:
                message = await websocket.receive_json()
                if message.get("type") != "move": continue
                current, now = state.presence[room_id][user["id"]], time.monotonic()
                if now-current["at"] < 0.095: continue
                values = [message.get(k) for k in ("x","z","rotation")]
                if not all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) for v in values): continue
                x,z,rotation = map(float,values)
                if abs(x)>1000 or abs(z)>1000 or abs(rotation)>math.tau*4: continue
                seq = message.get("seq")
                if seq is not None and (not isinstance(seq,int) or seq<=current["seq"]): continue
                elapsed=max(now-current["at"],0.1); distance=math.hypot(x-current["x"],z-current["z"])
                if distance/elapsed>25: continue
                current.update({"x":x,"z":z,"rotation":rotation,"at":now,"seq":seq if seq is not None else current["seq"]})
                await _broadcast_presence(state,room_id)
        tasks=[asyncio.create_task(sender()),asyncio.create_task(receiver())]
        try: await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks: task.cancel()
            service.unsubscribe(room_id,queue); state.sockets[key].discard(websocket)
            if not state.sockets[key]: state.presence[room_id].pop(user["id"],None)
            await _broadcast_presence(state,room_id)
            try: await websocket.close()
            except RuntimeError: pass

    app.state.rooms = state
    return app


async def _broadcast_presence(state, room_id):
    players=[{k:v for k,v in item.items() if k in {"user_id","x","z","rotation"}} for item in state.presence[room_id].values()]
    for sockets in [v for (rid,_),v in state.sockets.items() if rid==room_id]:
        for socket in tuple(sockets):
            try: await asyncio.wait_for(socket.send_json({"type":"presence","players":players}),2.0)
            except Exception: sockets.discard(socket)


def _error_response(status, code, message):
    from fastapi.responses import JSONResponse
    return JSONResponse({"error":{"code":code,"message":message}},status_code=status)
