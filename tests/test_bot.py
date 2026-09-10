"""Focused tests for the independent companion bot."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from parlay.bot import CompanionBot


class Rooms:
    def __init__(self) -> None:
        self.created = []
        self.actions = []

    async def create_personal(self, owner_id, invited_id=None, duration=7200):
        self.created.append((owner_id, invited_id, duration))
        return {"id": f"room-{len(self.created)}", "revision": 1}

    async def snapshot(self, room_id, owner_id):
        return {"id": room_id, "revision": 7}

    async def action(self, *args):
        self.actions.append(args)
        return {"id": args[0], "revision": 8}


class Compass:
    def __init__(self) -> None:
        self.preferences = []
        self.feedback_calls = []

    def set_preferences(self, *args):
        self.preferences.append(args)

    def reset_user(self, user_id):
        pass

    def delete_user(self, user_id):
        pass

    def recommend(self, user_id):
        return []

    def feedback(self, *args):
        self.feedback_calls.append(args)
        return True


class Event:
    def __init__(self, sender_id=10, *, private=True, group=False):
        self.sender_id = sender_id
        self.is_private = private
        self.is_group = group
        self.is_reply = False
        self.replies = []
        self.answers = []
        self.edits = []
        self.data = b""

    async def get_sender(self):
        return SimpleNamespace(id=self.sender_id, bot=False)

    async def reply(self, text, **kwargs):
        self.replies.append((text, kwargs))

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))

    async def edit(self, text, **kwargs):
        self.edits.append((text, kwargs))


def bot(tmp_path):
    config = SimpleNamespace(
        bot_db_path=tmp_path / "bot.db",
        bot_username="parlay_test_bot",
        mini_app_url="https://app.example.test",
        operator_id="99",
    )
    return CompanionBot(config, Rooms(), Compass(), lambda *args: None, client=SimpleNamespace())


def test_room_duration_exact_single_value_and_bounds(tmp_path):
    instance = bot(tmp_path)
    assert instance.parse_duration(None) == 7200
    assert instance.parse_duration("300s") == 300
    assert instance.parse_duration("5m") == 300
    assert instance.parse_duration("24h") == 86400
    for bad in ("", "2", "2 hours", "4m", "25h", "1h 2m"):
        with pytest.raises(ValueError):
            instance.parse_duration(bad)


@pytest.mark.asyncio
async def test_start_only_registers_dm_eligibility_not_compass_opt_in(tmp_path):
    instance = bot(tmp_path)
    event = Event()
    await instance._command_start(event, "")
    assert instance._state.eligible(10) is True
    assert instance.compass.preferences == []
    await instance._command_compass(event, "on")
    assert instance.compass.preferences == [("10", True)]


@pytest.mark.asyncio
async def test_inline_activation_is_owner_bound_and_deduplicated(tmp_path):
    instance = bot(tmp_path)
    token = instance._state.callback(10, "activate_room", {"duration": 7200})

    stranger = Event(11)
    stranger.data = instance._callback_data(token)
    await instance._on_callback(stranger)
    assert instance.rooms.created == []
    assert stranger.answers[-1][1]["alert"] is True

    owner = Event(10)
    owner.data = instance._callback_data(token)
    await instance._on_callback(owner)
    await instance._on_callback(owner)
    assert instance.rooms.created == [(10, None, 7200)]
    assert len(owner.edits) == 2
    assert "startapp=room-1" in owner.edits[-1][1]["buttons"].button.url


@pytest.mark.asyncio
async def test_feedback_callback_is_user_bound_and_idempotent(tmp_path):
    instance = bot(tmp_path)
    token = instance._state.callback(
        10, "compass_feedback", {"track_id": "a-very-large-track-id", "positive": True}
    )
    assert len(instance._callback_data(token)) <= 64
    owner = Event(10)
    owner.data = instance._callback_data(token)
    await instance._on_callback(owner)
    await instance._on_callback(owner)
    assert len(instance.compass.feedback_calls) == 1
    assert instance.compass.feedback_calls[0][-1] == f"bot:{token}"


@pytest.mark.asyncio
async def test_compass_requires_private_start_before_explicit_opt_in(tmp_path):
    instance = bot(tmp_path)
    event = Event(private=True)
    await instance._command_compass(event, "on")
    assert instance.compass.preferences == []
    assert "/start" in event.replies[-1][0]

    group = Event(private=False, group=True)
    await instance._command_start(group, "")
    assert instance._state.eligible(10) is False
