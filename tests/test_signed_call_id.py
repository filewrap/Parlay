"""Telegram call IDs are signed TL longs, not positive-only identifiers."""

from unittest.mock import Mock

import pytest
from telethon.extensions import BinaryReader
from telethon.tl.types import InputGroupCall

from parlay.rooms.service import RoomService
from parlay.runtime import Runtime


def runtime():
    return Runtime(
        chat_id=-1001234567890,
        generation=1,
        sessions=Mock(),
        bridge=Mock(),
        arbiter=Mock(),
        music=Mock(),
    )


@pytest.mark.parametrize("call_id", [-987654321012345678, -(2**63), -1, 1, 2**63 - 1])
def test_signed_call_id_preserved_and_rebinding_guarded(call_id):
    with BinaryReader(bytes(InputGroupCall(call_id, 123))) as reader:
        call = reader.tgread_object()
    instance = runtime()
    instance.bind_call_id(call.id)
    instance.bind_call_id(call.id)
    assert instance.call_id == call_id
    with pytest.raises(RuntimeError):
        instance.bind_call_id(2)
    assert instance.call_id == call_id


@pytest.mark.parametrize("call_id", [0, True, False, None, "123", 1.0, -(2**63) - 1, 2**63])
def test_invalid_call_id_rejected_without_mutation(call_id):
    instance = runtime()
    with pytest.raises(ValueError):
        instance.bind_call_id(call_id)
    assert instance.call_id is None


@pytest.mark.asyncio
async def test_negative_call_id_room_persistence_and_reuse(tmp_path):
    path = tmp_path / "rooms.sqlite3"
    service = RoomService(path, authority=lambda *_: True, member=lambda *_: True)
    first = await service.ensure_group(-1001234567890, -987654321012345678, 7)
    reloaded = RoomService(path, authority=lambda *_: True, member=lambda *_: True)
    same = await reloaded.ensure_group(-1001234567890, -987654321012345678, 7)
    assert first["id"] == same["id"]
    changed = await reloaded.ensure_group(-1001234567890, -987654321012345677, 7)
    assert changed["id"] != first["id"]
