"""Tests for unexpected call-drop handling (REQ-BOT-006).

The bridge must marshal a native-thread disconnect signal onto the asyncio loop
and invoke the async disconnect callback exactly once while active.
"""

from __future__ import annotations

import asyncio

from parlay.audio.bridge import RawAudioBridge


async def test_disconnect_signal_invokes_callback_when_active() -> None:
    fired = asyncio.Event()

    async def on_disconnect() -> None:
        fired.set()

    bridge = RawAudioBridge(client=object(), on_disconnect=on_disconnect)
    # Simulate the session being live without touching the native adapter.
    bridge._loop = asyncio.get_event_loop()
    bridge._active = True

    bridge._handle_disconnect()  # this is what the adapter calls on a drop
    await asyncio.wait_for(fired.wait(), timeout=1.0)
    assert fired.is_set()


async def test_disconnect_signal_ignored_when_inactive() -> None:
    calls = 0

    async def on_disconnect() -> None:
        nonlocal calls
        calls += 1

    bridge = RawAudioBridge(client=object(), on_disconnect=on_disconnect)
    bridge._loop = asyncio.get_event_loop()
    bridge._active = False  # no live session

    bridge._handle_disconnect()
    await asyncio.sleep(0.02)
    assert calls == 0


async def test_disconnect_noop_without_callback() -> None:
    bridge = RawAudioBridge(client=object())
    bridge._loop = asyncio.get_event_loop()
    bridge._active = True
    # Must not raise when no disconnect callback was provided.
    bridge._handle_disconnect()
    await asyncio.sleep(0.02)
