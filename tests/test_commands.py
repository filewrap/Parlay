"""Tests for the identity-gated, chat-aware command registry."""

from __future__ import annotations

import pytest

from parlay.commands import CommandHandler, ParsedCommand

OPERATOR = "12345"


def make_handler() -> CommandHandler:
    handler = CommandHandler(operator_id=OPERATOR, prefix="/")

    async def echo(cmd: ParsedCommand) -> str:
        return f"ok:{cmd.name}:{cmd.args}"

    for name in ("join", "leave", "start", "stop", "status", "play"):
        handler.register(name, echo)
    return handler


def test_parse_extracts_name_and_args() -> None:
    parsed = make_handler().parse("/play never gonna give you up", OPERATOR)
    assert parsed is not None
    assert parsed.name == "play"
    assert parsed.args == "never gonna give you up"


@pytest.mark.parametrize("text", ["hello", "/frobnicate", "/", "/ join", "/play@other song"])
def test_parse_ignores_unrelated_text(text: str) -> None:
    assert make_handler().parse(text, OPERATOR) is None


def test_identity_checks() -> None:
    handler = make_handler()
    assert handler.is_operator(OPERATOR)
    assert not handler.is_operator("99999")
    assert not handler.is_operator(None)
    handler.set_account(99999, "parlay")
    assert handler.is_operator(99999)
    assert handler.parse("/PLAY@Parlay\tsong", 99999).args == "song"


@pytest.mark.asyncio
async def test_dispatch() -> None:
    handler = make_handler()
    assert await handler.dispatch("/status", "99999") is None
    assert await handler.dispatch("/join mychat", OPERATOR) == "ok:join:mychat"
    assert await handler.dispatch("/whichmakenosense", OPERATOR) is None


@pytest.mark.parametrize(
    ("args", "group", "channel", "chat", "expected"),
    [
        ("", True, False, -101, -101),
        ("", True, True, -100202, -100202),
        ("", False, True, -100303, -100303),
        ("", False, False, 123, None),
        ("@target", True, False, -101, "@target"),
        ("-100123", False, False, 123, -100123),
    ],
)
def test_join_target(args, group, channel, chat, expected) -> None:
    command = ParsedCommand("join", args, OPERATOR, chat, group, channel)
    assert command.join_target() == expected


@pytest.mark.asyncio
async def test_dispatch_preserves_chat_context() -> None:
    handler = make_handler()
    received = []

    async def join(command):
        received.append(command)
        return "joined"

    handler.register("join", join)
    await handler.dispatch("/join", OPERATOR, chat_id=-101, is_group=True)
    assert received[0].join_target() == -101
