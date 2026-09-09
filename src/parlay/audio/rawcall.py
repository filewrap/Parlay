"""Thin adapter over the py-tgcalls raw group call.

This is the ONLY module that touches the tgcalls API surface. Everything else
in `parlay.audio` is pure Python and unit-tested. Isolating the binding here
means that if the concrete py-tgcalls call signatures differ on the target
runtime, only this file changes.

Expected API (py-tgcalls 2.x, raw group call):
    from pytgcalls import GroupCallFactory
    factory = GroupCallFactory(client, GroupCallFactory.MTPROTO_CLIENT_TYPE.TELETHON)
    raw = factory.get_raw_group_call(
        on_played_data=<callable(gc, length) -> bytes>,
        on_recorded_data=<callable(gc, frame, length) -> None>,
    )
    await raw.start(chat)
    await raw.stop()

Audio boundary: S16LE, 48 kHz, stereo, 10 ms frames.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Protocol

log = logging.getLogger(__name__)

RecordedHandler = Callable[[bytes, int], None]
PlayedHandler = Callable[[int], bytes]


class RawGroupCall(Protocol):
    """Structural type for the py-tgcalls raw group call we depend on."""

    async def start(self, chat: Any) -> None: ...
    async def stop(self) -> None: ...


class RawCallAdapter:
    """Builds and drives a raw group call, forwarding callbacks to handlers.

    The tgcalls callbacks pass the group-call instance as the first arg; we
    strip it and forward only the audio payload to the injected handlers so the
    rest of the bridge never sees a tgcalls type.
    """

    def __init__(
        self,
        client: Any,
        on_recorded: RecordedHandler,
        on_played: PlayedHandler,
    ) -> None:
        self._client = client
        self._on_recorded = on_recorded
        self._on_played = on_played
        self._raw: RawGroupCall | None = None

    def _build(self) -> RawGroupCall:
        # Imported lazily so the pure-Python core and its tests never require
        # the native wheel to be importable.
        from pytgcalls import GroupCallFactory

        factory = GroupCallFactory(
            self._client,
            GroupCallFactory.MTPROTO_CLIENT_TYPE.TELETHON,
        )

        def played(_gc: Any, length: int) -> bytes:
            return self._on_played(length)

        def recorded(_gc: Any, frame: bytes, length: int) -> None:
            self._on_recorded(frame, length)

        return factory.get_raw_group_call(
            on_played_data=played,
            on_recorded_data=recorded,
        )

    async def start(self, chat: Any) -> None:
        self._raw = self._build()
        await self._raw.start(chat)
        log.info("raw group call started")

    async def stop(self) -> None:
        if self._raw is None:
            return
        try:
            await self._raw.stop()
        finally:
            self._raw = None
            log.info("raw group call stopped")
