from pathlib import Path


gateway = Path("src/parlay/rooms/gateway.py")
text = gateway.read_text()
old = '    values.pop("signature", None)\n'
assert text.count(old) == 1, text.count(old)
gateway.write_text(text.replace(old, "", 1))

service = Path("src/parlay/rooms/service.py")
text = service.read_text()
old = '''            member = next((m for m in data["members"] if m["user_id"] == user_id), None)
            if action == "request_reentry" and user_id in data["kicked"] and not member:
                member = {"user_id": user_id, "role": "participant"}
            if not member or (user_id in data["kicked"] and action != "request_reentry"):
                raise RoomError("access_revoked", "Room access is not active", 403)
            if row["revision"] != expected_revision:
'''
new = '''            member = next((m for m in data["members"] if m["user_id"] == user_id), None)
            reentry_request = (
                action == "request_reentry" and user_id in data["kicked"] and member is None
            )
            if reentry_request:
                member = {"user_id": user_id, "role": "participant"}
            if not member or (user_id in data["kicked"] and not reentry_request):
                raise RoomError("access_revoked", "Room access is not active", 403)
            if not reentry_request and row["revision"] != expected_revision:
'''
assert text.count(old) == 1, text.count(old)
text = text.replace(old, new, 1)
old = '''            new_row = self._replace(row, revision, data)
            output = self._snapshot(new_row, user_id)
            await asyncio.to_thread(
'''
new = '''            new_row = self._replace(row, revision, data)
            if reentry_request:
                output = {"status": "pending"}
                publication = self._snapshot(new_row, row["owner_id"])
            else:
                output = self._snapshot(new_row, user_id)
                publication = output
            await asyncio.to_thread(
'''
assert text.count(old) == 1, text.count(old)
text = text.replace(old, new, 1)
action_start = text.index("    async def action(")
publish_at = text.index("        self._publish(room_id, output)\n", action_start)
text = text[:publish_at] + text[publish_at:].replace(
    "        self._publish(room_id, output)\n",
    "        self._publish(room_id, publication)\n",
    1,
)
service.write_text(text)

tests = Path("tests/test_rooms_gateway.py")
text = tests.read_text()
old = '    assert validate_init_data(signed(), TOKEN)["user"]["id"] == 1\n'
new = '''    assert validate_init_data(signed(), TOKEN)["user"]["id"] == 1
    modern = signed(extra={"signature": "modern-ed25519-signature"})
    assert validate_init_data(modern, TOKEN)["user"]["id"] == 1
    with pytest.raises(ValueError, match="signature"):
        validate_init_data(modern.replace("modern-ed25519-signature", "tampered"), TOKEN)
    with pytest.raises(ValueError, match="duplicate"):
        validate_init_data(modern + "&signature=other", TOKEN)
    with pytest.raises(ValueError, match="Telegram user"):
        validate_init_data(signed(extra={"user": "[]"}), TOKEN)
'''
assert text.count(old) == 1, text.count(old)
text = text.replace(old, new, 1)
old = '''                "expected_revision": kicked.json()["revision"],
                "action": "request_reentry",
'''
new = '''                "expected_revision": 0,
                "action": "request_reentry",
'''
assert text.count(old) == 1, text.count(old)
text = text.replace(old, new, 1)
old = '        assert request.json()["pending_reentry"] == []\n'
new = '''        assert request.json() == {"status": "pending"}
        denied = await client.get(f"/api/rooms/{room['id']}", headers=second)
        assert denied.status_code == 403
        denied_ticket = await client.post(
            "/api/ws-ticket", json={"room_id": room["id"]}, headers=second
        )
        assert denied_ticket.status_code == 403
'''
assert text.count(old) == 1, text.count(old)
tests.write_text(text.replace(old, new, 1))

tests = Path("tests/test_rooms_service.py")
text = tests.read_text()
old = '''    room = await service.action(room["id"], 2, "request", room["revision"], "request_reentry", {})
    assert notices and room["pending_reentry"] == []
    owner = await service.snapshot(room["id"], 1)
'''
new = '''    room_id = room["id"]
    room = await service.action(room_id, 2, "request", 0, "request_reentry", {})
    assert notices and room == {"status": "pending"}
    assert await service.action(room_id, 2, "request", 0, "request_reentry", {}) == room
    with pytest.raises(RoomError, match="access"):
        await service.snapshot(room_id, 2)
    owner = await service.snapshot(room_id, 1)
'''
assert text.count(old) == 1, text.count(old)
text = text.replace(old, new, 1)
text = text.replace(
    'room["id"], 1, "approve", owner["revision"]',
    'room_id, 1, "approve", owner["revision"]',
    1,
)
text = text.replace(
    'service.join(room["id"], user(2), "secret")',
    'service.join(room_id, user(2), "secret")',
    1,
)
tests.write_text(text)
