"""HTTP and real-network WebSocket tests for the listening-room gateway."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import socket
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast
from urllib.parse import urlencode

import httpx
import pytest
import uvicorn
import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from parlay.rooms.gateway import create_app, validate_init_data
from parlay.rooms.service import RoomService

TOKEN = "123:secret"
ORIGIN = "https://rooms.example"


def signed(uid: int = 1, when: float | None = None, extra: dict[str, str] | None = None) -> str:
    values = {
        "auth_date": str(int(time.time() if when is None else when)),
        "query_id": "q",
        "user": json.dumps({"id": uid, "first_name": f"U{uid}"}, separators=(",", ":")),
    }
    if extra:
        values.update(extra)
    check = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


@asynccontextmanager
async def running_app(app: Any) -> AsyncIterator[tuple[str, str]]:
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off", ws="websockets"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.01)
    assert server.started
    try:
        yield f"http://127.0.0.1:{port}", f"ws://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)


async def authenticate(client: httpx.AsyncClient, uid: int) -> dict[str, str]:
    response = await client.post("/api/auth", json={"init_data": signed(uid)})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['token']}"}


async def ticket(client: httpx.AsyncClient, headers: dict[str, str], room_id: str) -> str:
    response = await client.post("/api/ws-ticket", json={"room_id": room_id}, headers=headers)
    assert response.status_code == 200
    return str(response.json()["ticket"])


async def receive_type(connection: ClientConnection, kind: str) -> dict[str, Any]:
    for _ in range(20):
        message = json.loads(await asyncio.wait_for(connection.recv(), 2))
        if message.get("type") == kind:
            return message
    raise AssertionError(f"did not receive {kind}")


def test_official_init_data_signature_and_time_checks() -> None:
    assert validate_init_data(signed(), TOKEN)["user"]["id"] == 1
    with pytest.raises(ValueError, match="signature"):
        validate_init_data(signed().replace("U1", "bad"), TOKEN)
    with pytest.raises(ValueError, match="expired"):
        validate_init_data(signed(when=time.time() - 301), TOKEN)
    with pytest.raises(ValueError, match="future"):
        validate_init_data(signed(when=time.time() + 31), TOKEN)
    duplicate = signed() + "&auth_date=1"
    with pytest.raises(ValueError, match="duplicate"):
        validate_init_data(duplicate, TOKEN)


@pytest.mark.asyncio
async def test_http_bounds_replay_compass_identity_and_pending_reentry(tmp_path: Any) -> None:
    calls: list[tuple[str, tuple[Any, ...]]] = []

    class Compass:
        def recommend(self, user_id: str) -> list[dict[str, str]]:
            calls.append(("recommend", (user_id,)))
            return [{"id": user_id}]

    async def search(query: str) -> list[dict[str, Any]]:
        return [{"id": "x", "title": query, "source_url": "https://youtu.be/x"}]

    service = RoomService(tmp_path / "rooms.db", search=search)
    room = await service.create_personal(1, 2)
    app = create_app(service, TOKEN, [ORIGIN], compass=Compass(), search=search)
    async with (
        running_app(app) as (http_url, _),
        httpx.AsyncClient(base_url=http_url, headers={"Origin": ORIGIN}) as client,
    ):
        owner = await authenticate(client, 1)
        second = await authenticate(client, 2)
        joined_response = await client.post(
            f"/api/rooms/{room['id']}/join", json={}, headers=second
        )
        joined = joined_response.json()
        body = {
            "action_id": "appearance-1",
            "expected_revision": joined["revision"],
            "action": "appearance",
            "payload": {"avatar": "cat"},
        }
        first = await client.post(f"/api/rooms/{room['id']}/actions", json=body, headers=second)
        replay = await client.post(f"/api/rooms/{room['id']}/actions", json=body, headers=second)
        assert first.status_code == 200
        assert replay.json() == first.json()
        recommendations = await client.get("/api/compass", headers=second)
        assert recommendations.json()["recommendations"] == [{"id": "2"}]
        assert calls == [("recommend", ("2",))]
        oversized = await client.post(
            "/api/auth",
            content=b"x" * 65_537,
            headers={"Content-Type": "application/json", "Origin": ORIGIN},
        )
        assert oversized.status_code == 413
        current = (await client.get(f"/api/rooms/{room['id']}", headers=owner)).json()
        kicked = await client.post(
            f"/api/rooms/{room['id']}/actions",
            headers=owner,
            json={
                "action_id": "kick-2",
                "expected_revision": current["revision"],
                "action": "kick",
                "payload": {"user_id": 2},
            },
        )
        assert kicked.status_code == 200
        request = await client.post(
            f"/api/rooms/{room['id']}/actions",
            headers=second,
            json={
                "action_id": "reentry-2",
                "expected_revision": kicked.json()["revision"],
                "action": "request_reentry",
                "payload": {},
            },
        )
        assert request.status_code == 200
        assert request.json()["pending_reentry"] == []
        owner_view = (await client.get(f"/api/rooms/{room['id']}", headers=owner)).json()
        assert owner_view["pending_reentry"] == [2]


@pytest.mark.asyncio
async def test_real_two_client_cross_updates_movement_kick_and_ticket_replay(tmp_path: Any) -> None:
    service = RoomService(tmp_path / "rooms.db")
    room = await service.create_personal(1, 2)
    await service.join(room["id"], {"id": 2, "first_name": "U2"})
    app = create_app(service, TOKEN, [ORIGIN])
    async with (
        running_app(app) as (http_url, ws_url),
        httpx.AsyncClient(base_url=http_url, headers={"Origin": ORIGIN}) as client,
    ):
        owner = await authenticate(client, 1)
        second = await authenticate(client, 2)
        first_ticket = await ticket(client, owner, room["id"])
        second_ticket = await ticket(client, second, room["id"])
        async with (
            websockets.connect(
                f"{ws_url}/api/rooms/{room['id']}/ws?ticket={first_ticket}",
                origin=cast(Any, ORIGIN),
            ) as first_ws,
            websockets.connect(
                f"{ws_url}/api/rooms/{room['id']}/ws?ticket={second_ticket}",
                origin=cast(Any, ORIGIN),
            ) as second_ws,
        ):
            await receive_type(first_ws, "snapshot")
            await receive_type(second_ws, "snapshot")
            for _ in range(4):
                presence = await receive_type(first_ws, "presence")
                if {item["user_id"] for item in presence["players"]} == {1, 2}:
                    break
            else:
                pytest.fail("both connected users were not present")
            await asyncio.sleep(0.11)
            await second_ws.send(
                json.dumps({"type": "move", "x": 1, "z": 0, "rotation": 0, "seq": 1})
            )
            moved = await receive_type(first_ws, "presence")
            assert next(item for item in moved["players"] if item["user_id"] == 2)["x"] == 1
            await second_ws.send(
                json.dumps({"type": "move", "x": float("inf"), "z": 0, "rotation": 0})
            )
            current = (await client.get(f"/api/rooms/{room['id']}", headers=owner)).json()
            update = await client.post(
                f"/api/rooms/{room['id']}/actions",
                headers=owner,
                json={
                    "action_id": "appearance-owner",
                    "expected_revision": current["revision"],
                    "action": "appearance",
                    "payload": {"avatar": "fox"},
                },
            )
            assert update.status_code == 200
            cross_update = await receive_type(second_ws, "snapshot")
            assert (
                next(item for item in cross_update["snapshot"]["members"] if item["user_id"] == 1)[
                    "avatar"
                ]
                == "fox"
            )
            current = update.json()
            kicked = await client.post(
                f"/api/rooms/{room['id']}/actions",
                headers=owner,
                json={
                    "action_id": "kick-live",
                    "expected_revision": current["revision"],
                    "action": "kick",
                    "payload": {"user_id": 2},
                },
            )
            assert kicked.status_code == 200
            with pytest.raises(ConnectionClosed) as closed:
                await asyncio.wait_for(second_ws.recv(), 3)
            assert closed.value.code == 1008
        with pytest.raises(Exception):
            async with websockets.connect(
                f"{ws_url}/api/rooms/{room['id']}/ws?ticket={first_ticket}",
                origin=cast(Any, ORIGIN),
            ):
                pass


@pytest.mark.asyncio
async def test_websocket_session_expiry_disconnects_receiver(tmp_path: Any) -> None:
    service = RoomService(tmp_path / "rooms.db")
    room = await service.create_personal(1)
    app = create_app(service, TOKEN, [ORIGIN])
    async with (
        running_app(app) as (http_url, ws_url),
        httpx.AsyncClient(base_url=http_url, headers={"Origin": ORIGIN}) as client,
    ):
        owner = await authenticate(client, 1)
        ws_ticket = await ticket(client, owner, room["id"])
        async with websockets.connect(
            f"{ws_url}/api/rooms/{room['id']}/ws?ticket={ws_ticket}", origin=cast(Any, ORIGIN)
        ) as connection:
            await receive_type(connection, "snapshot")
            with app.state.rooms.connect() as db:
                db.execute("UPDATE room_sessions SET expires_at=?", (time.time() - 1,))
            connection_object = next(iter(app.state.rooms.sockets[room["id"]]))
            connection_object.expires_at = time.time() - 1
            closed: ConnectionClosed | None = None
            for _ in range(10):
                try:
                    await asyncio.wait_for(connection.recv(), 3)
                except ConnectionClosed as error:
                    closed = error
                    break
            assert closed is not None
            assert closed.code == 1008
