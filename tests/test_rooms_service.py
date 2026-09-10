import time

import pytest

from parlay.rooms.service import RoomError, RoomService

TRACK = {
    "id": "x",
    "title": "Track",
    "source_url": "https://www.youtube.com/watch?v=abc12345678",
    "youtube_id": "abc12345678",
    "duration": 60,
}


async def search(query):
    return [{**TRACK, "title": query}]


def user(uid):
    return {"id": uid, "first_name": f"U{uid}"}


@pytest.mark.asyncio
async def test_capacity_password_kick_reentry_and_replay(tmp_path):
    notices = []
    service = RoomService(
        tmp_path / "rooms.db", search=search, on_reentry=lambda *x: notices.append(x)
    )
    room = await service.create_personal(1, 2)
    room = await service.action(
        room["id"], 1, "settings", room["revision"], "settings", {"password": "secret"}
    )
    with pytest.raises(RoomError, match="password"):
        await service.join(room["id"], user(2), "bad")
    room = await service.join(room["id"], user(2), "secret")
    with pytest.raises(RoomError, match=r"capacity|invite-only"):
        await service.join(room["id"], user(3), "secret")
    room = await service.action(room["id"], 1, "kick", room["revision"], "kick", {"user_id": 2})
    with pytest.raises(RoomError, match="approve"):
        await service.join(room["id"], user(2), "secret")
    room_id = room["id"]
    room = await service.action(room_id, 2, "request", 0, "request_reentry", {})
    assert notices and room == {"status": "pending"}
    assert await service.action(room_id, 2, "request", 0, "request_reentry", {}) == room
    with pytest.raises(RoomError, match="access"):
        await service.snapshot(room_id, 2)
    owner = await service.snapshot(room_id, 1)
    owner = await service.action(
        room_id, 1, "approve", owner["revision"], "approve_reentry", {"user_id": 2}
    )
    joined = await service.join(room_id, user(2), "secret")
    owner = await service.snapshot(room_id, 1)
    played = await service.action(
        room_id, 1, "play", owner["revision"], "force_play", {"query": "Hello"}
    )
    replay = await service.action(
        room_id, 1, "play", owner["revision"], "force_play", {"query": "Hello"}
    )
    assert replay == played and joined["id"] == room_id
    with pytest.raises(RoomError, match="already used"):
        await service.action(
            room_id, 1, "play", owner["revision"], "force_play", {"query": "Other"}
        )


@pytest.mark.asyncio
async def test_expiry_restart_and_room_isolation(tmp_path):
    path = tmp_path / "rooms.db"
    service = RoomService(path, search=search)
    first = await service.create_personal(1, duration=300)
    second = await service.create_personal(2, duration=300)
    await service.action(first["id"], 1, "a", first["revision"], "force_play", {"query": "One"})
    assert (await service.snapshot(second["id"], 2))["playback"]["track"] is None
    with service._connect() as db:
        db.execute("UPDATE rooms SET expires_at=? WHERE id=?", (time.time() - 1, first["id"]))
    restarted = RoomService(path, search=search)
    await restarted.start()
    with pytest.raises(RoomError, match=r"expired|ended"):
        await restarted.snapshot(first["id"], 1)
    assert (await restarted.snapshot(second["id"], 2))["state"] == "active"
    await restarted.stop()


@pytest.mark.asyncio
async def test_group_membership_authority_and_isolation(tmp_path):
    def authority(uid, chat):
        return uid == 9

    def member(uid, chat):
        return uid in {9, 10}

    service = RoomService(tmp_path / "rooms.db", authority=authority, member=member, search=search)
    a = await service.ensure_group(100, 1, 9)
    b = await service.ensure_group(200, 1, 9)
    a = await service.join(a["id"], user(10))
    with pytest.raises(RoomError, match="authority"):
        await service.action(a["id"], 10, "close", a["revision"], "close", {})
    await service.publish_playback(
        100, {"track": TRACK, "status": "playing", "position_seconds": 0, "queue": []}
    )
    assert (await service.snapshot(b["id"], 9))["playback"]["track"] is None


@pytest.mark.asyncio
async def test_duration_override_expires_on_background_sweep(tmp_path):
    service = RoomService(tmp_path / "rooms.db")
    room = await service.create_personal(1, duration=600)
    room = await service.action(
        room["id"], 1, "shorten", room["revision"], "settings", {"duration": 300}
    )
    with service._connect() as db:
        row = db.execute("SELECT data_json FROM rooms WHERE id=?", (room["id"],)).fetchone()
        data = __import__("json").loads(row["data_json"])
        data["expires_override"] = time.time() - 1
        db.execute(
            "UPDATE rooms SET data_json=? WHERE id=?",
            (__import__("json").dumps(data), room["id"]),
        )
    service._expire_due()
    with pytest.raises(RoomError, match=r"expired|ended"):
        await service.snapshot(room["id"], 1)


@pytest.mark.asyncio
async def test_moderator_cannot_kick_a_moderator(tmp_path):
    service = RoomService(tmp_path / "rooms.db")
    room = await service.create_personal(1, 2)
    room = await service.join(room["id"], user(2))
    room = await service.action(
        room["id"], 1, "mod-2", room["revision"], "moderator", {"user_id": 2, "enabled": True}
    )
    with pytest.raises(RoomError, match="cannot be removed"):
        await service.action(room["id"], 2, "kick-2", room["revision"], "kick", {"user_id": 2})


@pytest.mark.asyncio
async def test_group_playback_failure_is_retryable_without_revision_change(tmp_path):
    attempts = 0

    async def playback(chat_id, action, payload):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("voice command failed")
        return {
            "track": payload["track"],
            "status": "playing",
            "position_seconds": 0,
            "queue": [],
        }

    service = RoomService(
        tmp_path / "rooms.db",
        playback=playback,
        authority=lambda uid, chat: uid == 9,
        member=lambda uid, chat: uid == 9,
        search=search,
    )
    room = await service.ensure_group(100, 1, 9)
    revision = room["revision"]
    with pytest.raises(RuntimeError, match="voice command failed"):
        await service.action(room["id"], 9, "play", revision, "force_play", {"query": "One"})
    assert (await service.snapshot(room["id"], 9))["revision"] == revision
    result = await service.action(room["id"], 9, "play", revision, "force_play", {"query": "One"})
    assert attempts == 2
    assert result["playback"]["track"]["title"] == "One"


@pytest.mark.asyncio
async def test_group_playback_actions_are_serialized_once(tmp_path):
    active = 0
    maximum = 0
    calls = []

    async def playback(chat_id, action, payload):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        calls.append(payload["track"]["title"])
        await __import__("asyncio").sleep(0.02)
        active -= 1
        return {
            "track": payload["track"],
            "status": "playing",
            "position_seconds": 0,
            "queue": [],
        }

    service = RoomService(
        tmp_path / "rooms.db",
        playback=playback,
        authority=lambda uid, chat: True,
        member=lambda uid, chat: True,
        search=search,
    )
    room = await service.ensure_group(100, 1, 9)
    first = __import__("asyncio").create_task(
        service.action(room["id"], 9, "one", room["revision"], "force_play", {"query": "One"})
    )
    await __import__("asyncio").sleep(0)
    with pytest.raises(RoomError, match="Refresh"):
        await service.action(room["id"], 9, "two", room["revision"], "force_play", {"query": "Two"})
    await first
    assert maximum == 1
    assert calls == ["One"]


@pytest.mark.asyncio
async def test_replay_rechecks_kick_and_expiry(tmp_path):
    service = RoomService(tmp_path / "rooms.db")
    room = await service.create_personal(1, 2)
    room = await service.join(room["id"], user(2))
    replayed = await service.action(
        room["id"], 2, "look", room["revision"], "appearance", {"avatar": "cat"}
    )
    owner = await service.snapshot(room["id"], 1)
    await service.action(room["id"], 1, "kick-replay", owner["revision"], "kick", {"user_id": 2})
    with pytest.raises(RoomError, match="not active"):
        await service.action(
            room["id"], 2, "look", room["revision"], "appearance", {"avatar": "cat"}
        )

    expiring = await service.create_personal(3, duration=300)
    historical = await service.action(
        expiring["id"], 3, "saved", expiring["revision"], "appearance", {"avatar": "fox"}
    )
    with service._connect() as db:
        db.execute("UPDATE rooms SET expires_at=? WHERE id=?", (time.time() - 1, expiring["id"]))
    with pytest.raises(RoomError, match="expired"):
        await service.action(
            expiring["id"], 3, "saved", expiring["revision"], "appearance", {"avatar": "fox"}
        )
    assert replayed["id"] == room["id"] and historical["id"] == expiring["id"]


@pytest.mark.asyncio
async def test_group_recovery_and_live_authority_permissions(tmp_path):
    authorities = {9}
    service = RoomService(
        tmp_path / "rooms.db",
        authority=lambda uid, chat: uid in authorities,
        member=lambda uid, chat: uid in {9, 10},
    )
    room = await service.ensure_group(100, 1, 9)
    joined = await service.join(room["id"], user(10))
    assert joined["permissions"]["manage_settings"] is False
    authorities.clear()
    authorities.add(10)
    owner_view = await service.snapshot(room["id"], 9)
    authority_view = await service.snapshot(room["id"], 10)
    assert owner_view["permissions"]["manage_settings"] is False
    assert authority_view["permissions"]["manage_settings"] is True
    await service.set_recovering(100, "transient voice failure")
    recovering = await service.snapshot(room["id"], 10)
    assert recovering["state"] == "recovering"
    before = recovering["revision"]
    await service.publish_playback(
        100, {"track": TRACK, "status": "playing", "position_seconds": 0, "queue": []}
    )
    active = await service.snapshot(room["id"], 10)
    assert active["state"] == "active" and active["revision"] == before + 1
    await service.publish_playback(
        100, {"track": TRACK, "status": "playing", "position_seconds": 0, "queue": []}
    )
    assert (await service.snapshot(room["id"], 10))["revision"] == active["revision"]


@pytest.mark.asyncio
async def test_action_callback_success_replay_and_failure_isolation(tmp_path):
    events = []

    async def on_action(room_id, user_id, action, track, event_id):
        events.append((room_id, user_id, action, track, event_id))
        if action == "force_play":
            raise RuntimeError("ML unavailable")

    service = RoomService(tmp_path / "rooms.db", search=search, on_action=on_action)
    room = await service.create_personal(1)
    queued = await service.action(
        room["id"], 1, "select-1", room["revision"], "queue_add", {"query": "Selected"}
    )
    assert (
        await service.action(
            room["id"], 1, "select-1", room["revision"], "queue_add", {"query": "Selected"}
        )
        == queued
    )
    assert len(events) == 1
    assert events[0][:3] == (room["id"], 1, "queue_add")
    assert events[0][3]["youtube_id"] == "abc12345678"
    assert events[0][4] == f"{room['id']}:1:select-1"
    played = await service.action(
        room["id"], 1, "play-1", queued["revision"], "force_play", {"query": "Played"}
    )
    assert played["playback"]["track"]["title"] == "Played"
    assert (
        await service.action(
            room["id"], 1, "play-1", queued["revision"], "force_play", {"query": "Played"}
        )
        == played
    )
    assert len(events) == 2


@pytest.mark.asyncio
async def test_action_callback_ignores_unauthorized_and_failed_actions(tmp_path):
    events = []

    async def on_action(*args):
        events.append(args)

    service = RoomService(tmp_path / "rooms.db", search=search, on_action=on_action)
    room = await service.create_personal(1)
    with pytest.raises(RoomError):
        await service.action(
            room["id"], 2, "denied", room["revision"], "force_play", {"query": "Denied"}
        )

    async def no_results(query):
        return []

    failed = RoomService(tmp_path / "failed.db", search=no_results, on_action=on_action)
    other = await failed.create_personal(1)
    with pytest.raises(RoomError, match="No playable"):
        await failed.action(
            other["id"], 1, "missing", other["revision"], "queue_add", {"query": "Missing"}
        )
    assert events == []
