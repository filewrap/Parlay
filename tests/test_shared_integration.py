"""Bounded end-to-end coverage for the shared room application boundary."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sqlite3
import time
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest

from parlay.app import ParlayApp
from parlay.ml import CompassService
from parlay.rooms.gateway import create_app
from parlay.rooms.service import RoomService

BOT_TOKEN = "123456:test-secret"
ORIGIN = "https://rooms.example"
CHAT_ID = -100123
CALL_ID = 987654
ORIGINAL_OWNER = 101
CURRENT_GROUP_OWNER = 202
TRACK_ID = "abcdefghijk"
TRACK_URL = f"https://www.youtube.com/watch?v={TRACK_ID}"


def signed_init_data(user_id: int) -> str:
    values = {
        "auth_date": str(int(time.time())),
        "query_id": f"query-{user_id}",
        "user": json.dumps(
            {"id": user_id, "first_name": f"Actor {user_id}"}, separators=(",", ":")
        ),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(values.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


async def authenticate(client: httpx.AsyncClient, user_id: int) -> dict[str, str]:
    response = await client.post("/api/auth", json={"init_data": signed_init_data(user_id)})
    assert response.status_code == 200
    assert response.json()["user"]["id"] == user_id
    return {"Authorization": f"Bearer {response.json()['token']}"}


class FakeTelegramMembership:
    """A live Telegram membership boundary whose group owner can change."""

    def __init__(self) -> None:
        self.members = {ORIGINAL_OWNER, CURRENT_GROUP_OWNER}
        self.owner = CURRENT_GROUP_OWNER
        self.checks: list[tuple[str, int, int]] = []

    async def member(self, user_id: int, chat_id: int) -> bool:
        self.checks.append(("member", user_id, chat_id))
        return chat_id == CHAT_ID and user_id in self.members

    async def authority(self, user_id: int, chat_id: int) -> bool:
        self.checks.append(("authority", user_id, chat_id))
        return chat_id == CHAT_ID and user_id == self.owner


class FakeRuntimeRegistry:
    """A deterministic media boundary that returns authoritative TV state."""

    def __init__(self) -> None:
        self.runtime = SimpleNamespace(call_id=CALL_ID)
        self.calls: list[tuple[int, str, Any]] = []
        self.snapshot = {
            "track": None,
            "status": "idle",
            "position_seconds": 0.0,
            "server_time": time.time(),
            "queue": [],
        }

    def get(self, chat_id: int) -> Any:
        return self.runtime if chat_id == CHAT_ID else None

    async def command(self, chat_id: int, action: str, payload: Any = None) -> dict[str, Any]:
        self.calls.append((chat_id, action, payload))
        assert chat_id == CHAT_ID
        if action in {"play", "force_play"}:
            assert payload == TRACK_URL
            assert "token=" not in payload and "cdn.example" not in payload
            self.snapshot = {
                "track": {
                    "id": TRACK_ID,
                    "title": "Authoritative TV title",
                    "source_url": TRACK_URL,
                    "youtube_id": TRACK_ID,
                    "duration": 180.0,
                },
                "status": "playing",
                "position_seconds": 4.0,
                "server_time": time.time(),
                "queue": [],
            }
        elif action == "pause":
            self.snapshot = {**self.snapshot, "status": "paused", "server_time": time.time()}
        else:
            raise AssertionError(f"unexpected runtime action: {action}")
        return json.loads(json.dumps(self.snapshot))


@pytest.mark.asyncio
async def test_two_actor_gateway_room_runtime_and_compass_integration(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("parlay.app.build_client", lambda _config: object())
    config = SimpleNamespace(
        operator_id=str(ORIGINAL_OWNER),
        command_prefix="/",
        pot_provider_url="",
        max_concurrent_calls=2,
        activity_db_path=str(tmp_path / "activity.db"),
    )
    app = ParlayApp(config)
    membership = FakeTelegramMembership()
    registry = FakeRuntimeRegistry()
    app.registry = registry
    app._member = membership.member
    app._authority = membership.authority
    app.compass = CompassService(tmp_path / "compass.db", tmp_path / "models")
    app.compass.set_preferences(str(CURRENT_GROUP_OWNER), True, count=1)

    async def search(query: str) -> list[dict[str, Any]]:
        return [
            {
                "id": TRACK_ID,
                "title": query,
                "source_url": TRACK_URL,
                "youtube_id": TRACK_ID,
                "duration": 180,
                "stream_url": "https://cdn.example/audio?token=secret",
                "cookies": "private",
            }
        ]

    callback_reads: list[dict[str, Any]] = []

    async def successful_action(
        room_id: str,
        user_id: int,
        action: str,
        track: dict[str, Any],
        event_id: str,
    ) -> None:
        await app._room_action_event(room_id, user_id, action, track, event_id)
        callback_reads.append(await app.rooms.snapshot(room_id, user_id))

    app.rooms = RoomService(
        tmp_path / "rooms.db",
        app._room_playback,
        authority=app._authority,
        member=app._member,
        search=search,
        on_action=successful_action,
    )
    room = await app.rooms.ensure_group(CHAT_ID, CALL_ID, ORIGINAL_OWNER)
    gateway = create_app(app.rooms, BOT_TOKEN, [ORIGIN], app.compass, search)
    transport = httpx.ASGITransport(app=gateway)

    async with httpx.AsyncClient(
        transport=transport, base_url="https://rooms.example", headers={"Origin": ORIGIN}
    ) as client:
        original = await authenticate(client, ORIGINAL_OWNER)
        current_owner = await authenticate(client, CURRENT_GROUP_OWNER)
        invalid = signed_init_data(303).replace("Actor+303", "Tampered")
        assert (await client.post("/api/auth", json={"init_data": invalid})).status_code == 401

        original_view = (await client.get(f"/api/rooms/{room['id']}", headers=original)).json()
        assert original_view["owner_id"] == ORIGINAL_OWNER
        assert original_view["permissions"]["control"] is False
        joined = await client.post(f"/api/rooms/{room['id']}/join", json={}, headers=current_owner)
        assert joined.status_code == 200
        admitted = joined.json()
        current_view = (
            await client.get(f"/api/rooms/{room['id']}", headers=current_owner)
        ).json()
        assert admitted["id"] == room["id"]
        assert current_view["permissions"]["control"] is True

        searched = await client.get(
            "/api/search", params={"q": "Shared song"}, headers=current_owner
        )
        assert searched.status_code == 200
        assert searched.json()["tracks"] == [
            {
                "id": TRACK_ID,
                "title": "Shared song",
                "source_url": TRACK_URL,
                "youtube_id": TRACK_ID,
                "duration": 180.0,
            }
        ]

        queued = await asyncio.wait_for(
            client.post(
                f"/api/rooms/{room['id']}/actions",
                headers=current_owner,
                json={
                    "action_id": "queue-1",
                    "expected_revision": current_view["revision"],
                    "action": "queue_add",
                    "payload": {"query": "Shared song"},
                },
            ),
            timeout=2,
        )
        assert queued.status_code == 200
        authoritative = queued.json()
        assert authoritative["playback"]["track"]["title"] == "Authoritative TV title"
        assert authoritative["playback"]["status"] == "playing"
        assert callback_reads and callback_reads[0]["revision"] == authoritative["revision"]
        assert registry.calls == [(CHAT_ID, "play", TRACK_URL)]

        denied = await client.post(
            f"/api/rooms/{room['id']}/actions",
            headers=original,
            json={
                "action_id": "old-owner-pause",
                "expected_revision": authoritative["revision"],
                "action": "pause",
                "payload": {},
            },
        )
        assert denied.status_code == 403

        paused = await client.post(
            f"/api/rooms/{room['id']}/actions",
            headers=current_owner,
            json={
                "action_id": "current-owner-pause",
                "expected_revision": authoritative["revision"],
                "action": "pause",
                "payload": {},
            },
        )
        assert paused.status_code == 200
        assert paused.json()["playback"]["status"] == "paused"
        latest = (await client.get(f"/api/rooms/{room['id']}", headers=current_owner)).json()
        assert latest["playback"]["status"] == "paused"

        opted_out = await client.get("/api/compass", headers=original)
        opted_in = await client.get("/api/compass", headers=current_owner)
        assert opted_out.json()["recommendations"] == []
        assert opted_in.json()["recommendations"][0]["id"] == TRACK_ID

    with sqlite3.connect(app.compass.store.path) as db:
        events = db.execute(
            "SELECT user_id,track_id,event_type,event_id,context_json FROM events"
        ).fetchall()
    assert len(events) == 1
    actor, track_id, event_type, event_id, context_json = events[0]
    assert (actor, track_id, event_type) == (str(CURRENT_GROUP_OWNER), TRACK_ID, "play")
    assert event_id == f"{room['id']}:{CURRENT_GROUP_OWNER}:queue-1"
    assert json.loads(context_json) == {"room_id": room["id"], "origin": "queue_add"}
    assert registry.calls[-1] == (CHAT_ID, "pause", {})
    assert ("authority", CURRENT_GROUP_OWNER, CHAT_ID) in membership.checks
