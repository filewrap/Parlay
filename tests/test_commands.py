"""Tests for the operator-gated command handler."""

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
    handler = make_handler()
    parsed = handler.parse("/play never gonna give you up", OPERATOR)
    assert parsed is not None
    assert parsed.name == "play"
    assert parsed.args == "never gonna give you up"


def test_parse_ignores_non_prefixed_text() -> None:
    handler = make_handler()
    assert handler.parse("hello there", OPERATOR) is None


def test_is_operator_matches_only_configured_identity() -> None:
    handler = make_handler()
    assert handler.is_operator(OPERATOR) is True
    assert handler.is_operator("99999") is False
    assert handler.is_operator(None) is False


@pytest.mark.asyncio
async def test_dispatch_ignores_non_operator() -> None:
    handler = make_handler()
    assert await handler.dispatch("/status", "99999") is None


@pytest.mark.asyncio
async def test_dispatch_routes_operator_command() -> None:
    handler = make_handler()
    assert await handler.dispatch("/join mychat", OPERATOR) == "ok:join:mychat"


@pytest.mark.asyncio
async def test_dispatch_unknown_command() -> None:
    handler = make_handler()
    reply = await handler.dispatch("/frobnicate", OPERATOR)
    assert reply is not None and "Unknown command" in reply
