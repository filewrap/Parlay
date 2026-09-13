"""Adapter over py-tgcalls 2.3.x and its NTgCalls 2.x backend.

This is the only module that imports pytgcalls. A single PyTgCalls engine is
shared by all calls using the same MTProto client. Each adapter owns one chat,
one update handler, and one paced raw-audio pump.

Inbound diagnostics: to trace "call joined but no audio reaches the AI" cases,
the update handler logs the first StreamFrames it sees, the first forwarded
incoming frame, and any StreamFrames dropped by the direction filter. Set the
`parlay.audio.rawcall` logger to DEBUG for per-frame detail.

NTgCalls reports incoming participant audio with the INCOMING direction and
tags the device as MICROPHONE (the participant's mic), not SPEAKER. We forward
every INCOMING frame regardless of device and drop OUTGOING frames (our own
playout looped back).

Participant updates (join/leave) arrive as UpdatedGroupCallParticipant and are
forwarded through an optional on_participant callback so the app can post an
in-call join notice and track who is speaking. Self-mute is exposed through
mute()/unmute(), which drive our own outgoing stream via the engine.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

log = logging.getLogger(__name__)

SAMPLE_RATE = 48_000
CHANNELS = 2
BYTES_PER_SAMPLE = 2
FRAME_MS = 10
FRAME_BYTES = SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE * FRAME_MS // 1000

RecordedHandler = Callable[[bytes, int], None]
PlayedHandler = Callable[[int], bytes]
DisconnectHandler = Callable[[], None]
# (action, user_id): action is "joined" or "left".
ParticipantHandler = Callable[[str, int], None]

# Log an inbound-frame summary every this many forwarded frames.
_RECV_LOG_EVERY = 500

# PyTgCalls registers MTProto handlers on its client. Sharing one engine avoids
# duplicate registration. Startup is protected per client, so unrelated clients
# never wait for each other's network I/O.
_apps: dict[int, Any] = {}
_started: set[int] = set()
_start_locks: dict[int, asyncio.Lock] = {}


def _load_api() -> SimpleNamespace:
    """Import the exact py-tgcalls surface used by the raw adapter."""
    from pytgcalls import PyTgCalls
    from pytgcalls.types import (
        AudioQuality,
        ChatUpdate,
        Device,
        Direction,
        ExternalMedia,
        MediaStream,
        RecordStream,
        StreamFrames,
    )

    return SimpleNamespace(
        PyTgCalls=PyTgCalls,
        AudioQuality=AudioQuality,
        ChatUpdate=ChatUpdate,
        Device=Device,
        Direction=Direction,
        ExternalMedia=ExternalMedia,
        MediaStream=MediaStream,
        RecordStream=RecordStream,
        StreamFrames=StreamFrames,
    )


def _get_app(api: SimpleNamespace, client: Any) -> Any:
    key = id(client)
    app = _apps.get(key)
    if app is None:
        app = api.PyTgCalls(client)
        _apps[key] = app
    return app


async def _start_app_once(api: SimpleNamespace, client: Any) -> Any:
    """Start the client's shared engine exactly once under concurrent joins."""
    key = id(client)
    lock = _start_locks.setdefault(key, asyncio.Lock())
    async with lock:
        app = _get_app(api, client)
        if key not in _started:
            await app.start()
            _started.add(key)
        return app


class RawCallAdapter:
    """Join one group call and move 10 ms PCM frames in both directions."""

    def __init__(
        self,
        client: Any,
        on_recorded: RecordedHandler,
        on_played: PlayedHandler,
        on_disconnect: DisconnectHandler | None = None,
        on_participant: ParticipantHandler | None = None,
    ) -> None:
        self._client = client
        self._on_recorded = on_recorded
        self._on_played = on_played
        self._on_disconnect = on_disconnect
        self._on_participant = on_participant
        self._api: SimpleNamespace | None = None
        self._app: Any = None
        self._chat_id: int | None = None
        self._pump_task: asyncio.Task[None] | None = None
        self._saw_stream_frames = False
        self._recorded_frames = 0
        self._muted = False

    async def start(self, chat: Any) -> None:
        api = self._api = _load_api()
        app = await _start_app_once(api, self._client)
        self._chat_id = await app.resolve_chat_id(chat)
        self._app = app
        app.add_handler(self._handle_update)
        try:
            await app.play(
                self._chat_id,
                api.MediaStream(
                    api.ExternalMedia.AUDIO,
                    audio_parameters=api.AudioQuality.HIGH,
                ),
            )
            await app.record(
                self._chat_id,
                api.RecordStream(audio=True, audio_parameters=api.AudioQuality.HIGH),
            )
        except BaseException:
            app.remove_handler(self._handle_update)
            self._app = None
            self._chat_id = None
            raise
        self._pump_task = asyncio.create_task(self._pump())
        log.info("joined group call %s (external audio in, recording out)", self._chat_id)

    async def stop(self) -> None:
        if self._app is None:
            return
        app, self._app = self._app, None
        if self._pump_task is not None:
            self._pump_task.cancel()
            try:
                await self._pump_task
            except asyncio.CancelledError:
                pass
            self._pump_task = None
        app.remove_handler(self._handle_update)
        chat_id, self._chat_id = self._chat_id, None
        try:
            if chat_id is not None:
                await app.leave_call(chat_id)
        except Exception:
            log.warning("leave_call failed; probably already out of the call", exc_info=True)
        log.info(
            "left group call %s (forwarded %d inbound frames)", chat_id, self._recorded_frames
        )

    # --- self-mute ------------------------------------------------------------
    async def mute(self) -> None:
        """Mute our own outgoing stream. Degrades to a no-op on API mismatch."""
        await self._set_muted(True)

    async def unmute(self) -> None:
        """Unmute our own outgoing stream. Degrades to a no-op on API mismatch."""
        await self._set_muted(False)

    async def _set_muted(self, muted: bool) -> None:
        if self._app is None or self._chat_id is None or muted == self._muted:
            return
        method = getattr(self._app, "mute" if muted else "unmute", None)
        if method is None:
            return
        try:
            await method(self._chat_id)
            self._muted = muted
        except Exception:
            log.debug("self-%s failed", "mute" if muted else "unmute", exc_info=True)

    async def _pump(self) -> None:
        api = self._api
        assert api is not None
        loop = asyncio.get_running_loop()
        next_at = loop.time()
        while True:
            data = self._on_played(FRAME_BYTES)
            if data and self._app is not None and self._chat_id is not None:
                try:
                    await self._app.send_frame(self._chat_id, api.Device.MICROPHONE, data)
                except Exception:
                    log.debug("send_frame failed", exc_info=True)
            next_at += FRAME_MS / 1000
            delay = next_at - loop.time()
            if delay < 0:
                next_at = loop.time()
                delay = 0.0
            await asyncio.sleep(delay)

    async def _handle_update(self, _client: Any, update: Any) -> None:
        api = self._api
        if api is None or getattr(update, "chat_id", None) != self._chat_id:
            return
        if isinstance(update, api.StreamFrames):
            self._handle_frames(update)
        elif isinstance(update, api.ChatUpdate):
            if update.status & api.ChatUpdate.Status.LEFT_CALL:
                log.warning("group call ended or dropped (status=%s)", update.status)
                if self._on_disconnect is not None:
                    self._on_disconnect()
        else:
            self._handle_participant(update)

    def _handle_frames(self, update: Any) -> None:
        api = self._api
        assert api is not None
        if not self._saw_stream_frames:
            self._saw_stream_frames = True
            log.info(
                "first StreamFrames update (direction=%s, device=%s, frames=%d)",
                getattr(update, "direction", "?"),
                getattr(update, "device", "?"),
                len(getattr(update, "frames", []) or []),
            )
        # NTgCalls tags incoming participant audio as MICROPHONE, not SPEAKER,
        # so filter on direction only and forward every incoming frame. OUTGOING
        # frames are our own playout looped back; drop them.
        if update.direction & api.Direction.INCOMING:
            for frame in update.frames:
                self._on_recorded(frame.frame, len(frame.frame))
                self._recorded_frames += 1
                if self._recorded_frames == 1:
                    log.info(
                        "first inbound frame forwarded (%d bytes, device=%s)",
                        len(frame.frame),
                        getattr(update, "device", "?"),
                    )
                elif self._recorded_frames % _RECV_LOG_EVERY == 0:
                    log.info("inbound frames forwarded: %d", self._recorded_frames)
        else:
            log.debug(
                "dropped outgoing StreamFrames (dir=%s, dev=%s)",
                getattr(update, "direction", "?"),
                getattr(update, "device", "?"),
            )

    def _handle_participant(self, update: Any) -> None:
        """Forward join/leave for an UpdatedGroupCallParticipant-shaped update.

        The concrete type name varies across pytgcalls builds, so this matches
        structurally: an object carrying a `participant` with a `user_id` and an
        `action`. Anything else is ignored.
        """
        if self._on_participant is None:
            return
        participant = getattr(update, "participant", None)
        user_id = getattr(participant, "user_id", None)
        if not isinstance(user_id, int):
            return
        action = getattr(update, "action", None)
        name = getattr(action, "name", str(action)) if action is not None else ""
        text = name.upper()
        if "LEFT" in text:
            self._on_participant("left", user_id)
        elif "JOIN" in text:
            self._on_participant("joined", user_id)
