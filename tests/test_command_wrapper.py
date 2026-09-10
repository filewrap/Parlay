"""Exercise actual Telethon message-event construction and wrapper dispatch."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telethon import events
from telethon.tl import types

from parlay.command_wrapper import TelegramCommandWrapper
from parlay.commands import CommandHandler


def make_event(text="/join", sender=7, outgoing=False, peer=None, message_id=1):
    message = types.Message(
        id=message_id,
        peer_id=peer or types.PeerChat(101),
        from_id=types.PeerUser(sender),
        message=text,
        out=outgoing,
    )
    event = events.NewMessage.Event(message)
    event.reply = AsyncMock(return_value=SimpleNamespace(id=100 + message_id))
    return event


def setup_wrapper():
    commands = CommandHandler("8")
    commands.set_account(7)
    handler = AsyncMock(return_value="joined")
    commands.register("join", handler)
    client = Mock()
    return TelegramCommandWrapper(client, commands), handler


@pytest.mark.asyncio
@pytest.mark.parametrize(("sender", "outgoing"), [(7, True), (8, False)])
async def test_self_and_operator_commands_preserve_chat(sender, outgoing):
    wrapper, handler = setup_wrapper()
    event = make_event(sender=sender, outgoing=outgoing)
    await wrapper.handle(event)
    handler.assert_awaited_once()
    assert handler.call_args.args[0].join_target() == -101
    event.reply.assert_awaited_once()


@pytest.mark.asyncio
async def test_unknown_unauthorized_and_duplicate_are_silent():
    wrapper, handler = setup_wrapper()
    for event in [make_event("/nonsense"), make_event(sender=99, outgoing=True)]:
        await wrapper.handle(event)
        event.reply.assert_not_awaited()
    event = make_event()
    await wrapper.handle(event)
    await wrapper.handle(event)
    handler.assert_awaited_once()
    event.reply.assert_awaited_once()


@pytest.mark.asyncio
async def test_channel_context_and_private_context():
    wrapper, handler = setup_wrapper()
    await wrapper.handle(make_event(peer=types.PeerChannel(202)))
    assert handler.call_args.args[0].join_target() == -1000000000202
    await wrapper.handle(make_event(peer=types.PeerUser(8), message_id=2))
    assert handler.call_args.args[0].join_target() is None


def test_installs_both_directions():
    wrapper, _ = setup_wrapper()
    wrapper.install()
    builder = wrapper.client.add_event_handler.call_args.args[1]
    assert builder.incoming is None
    assert builder.outgoing is None
    assert builder.forwards is False
