"""Focused regression tests for application lifecycle boundaries."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from parlay.app import ParlayApp


def app():
    a = object.__new__(ParlayApp)
    a.config = SimpleNamespace(operator_id="7")
    a.client = Mock(remove_event_handler=Mock(), disconnect=AsyncMock())
    a.command_wrapper = Mock(uninstall=Mock())
    a.registry = Mock(close=AsyncMock())
    a.compass = a.bot = a.rooms = a.activity = None
    a._activity_ready = False
    a._jobs = set()
    a._recoveries = {}
    a._shutting_down = False
    a._server = None
    return a


@pytest.mark.asyncio
async def test_authorization_failure_cleans_up(monkeypatch):
    a = app()
    monkeypatch.setattr(
        "parlay.app.start_authorized", AsyncMock(side_effect=RuntimeError("authorization failed"))
    )
    with pytest.raises(RuntimeError, match="authorization failed"):
        await a.run()
    a.registry.close.assert_awaited_once()
    a.client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_cleanup_attempts_all_and_preserves_startup_error(monkeypatch):
    a = app()
    a.compass = Mock(stop=AsyncMock(side_effect=RuntimeError("stop")))
    a.bot = Mock(stop=AsyncMock())
    a.rooms = Mock(stop=AsyncMock())
    a.activity = Mock(stop=AsyncMock())
    monkeypatch.setattr("parlay.app.start_authorized", AsyncMock(side_effect=ValueError("startup")))
    with pytest.raises(ValueError, match="startup"):
        await a.run()
    a.bot.stop.assert_awaited_once()
    a.rooms.stop.assert_awaited_once()
    a.activity.stop.assert_awaited_once()
    a.client.disconnect.assert_awaited_once()


def test_numeric_operator_is_id():
    assert ParlayApp._operator_reference("7") == 7
    assert ParlayApp._operator_reference("+15551234567") == "+15551234567"


@pytest.mark.asyncio
async def test_none_runtime_callback_preserves_replacement():
    a = app()
    current = None
    a.registry.get = lambda _: current
    a.registry.leave = AsyncMock()
    a.rooms = Mock(end_group=AsyncMock())
    pending = []
    a._spawn = lambda item: pending.append(item)
    await a._queue_activity_unavailable(-1, "gone")
    current = object()
    await pending[0]
    a.registry.leave.assert_not_awaited()
    a.rooms.end_group.assert_not_awaited()


@pytest.mark.asyncio
async def test_old_recovery_preserves_new_mapping(monkeypatch):
    a = app()
    entered = asyncio.Event()

    async def sleep(_):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(asyncio, "sleep", sleep)
    old = asyncio.create_task(a._recover(-1))
    a._recoveries[-1] = old
    await entered.wait()
    new = asyncio.create_task(asyncio.Event().wait())
    a._recoveries[-1] = new
    old.cancel()
    await asyncio.gather(old, return_exceptions=True)
    assert a._recoveries[-1] is new
    new.cancel()
    await asyncio.gather(new, return_exceptions=True)


@pytest.mark.asyncio
async def test_server_timeout_cancels(monkeypatch):
    a = app()
    a._server = SimpleNamespace(should_exit=False)
    task = asyncio.create_task(asyncio.Event().wait())
    seen = []

    async def timeout(awaitable, *, timeout):
        del awaitable
        seen.append(timeout)
        raise TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", timeout)
    await a._shutdown_server(task)
    assert a._server.should_exit
    assert seen == [15]
    assert task.cancelled()
