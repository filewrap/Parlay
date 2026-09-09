"""ProviderSessionManager: drives a VoiceProvider across the bridge.

On engage it opens the provider session, subscribes to the Captured Stream as a
16 kHz mono consumer, forwards each captured chunk upstream, and drains the
provider's reply events into the Playback Sink. On disengage it detaches and
closes the session. Interruptions flush pending playback immediately; an
unrecoverable session loss reports to the Operator and disengages.

This module is provider-agnostic (talks to `VoiceProvider`) and bridge-agnostic
(talks to the `CapturedSource` / `PlaybackSink` protocols), so it is fully unit
testable with fakes. `RawAudioBridge` satisfies both protocols structurally.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from ..audio.frames import AudioChunk
from .provider import ProviderError, ReplyEvent, ReplyEventKind, VoiceProvider

log = logging.getLogger(__name__)

# Called when the pipeline disengages itself after an unrecoverable loss, so the
# app can report to the Operator and reconcile session state. The argument is a
# human-readable reason.
LossHandler = Callable[[str], Awaitable[None]]


class CapturedSource(Protocol):
    """The inbound side of the bridge: subscribe/unsubscribe Captured Stream."""

    def subscribe(
        self,
        callback: Callable[[AudioChunk], Awaitable[None]],
        rate: int,
        channels: int,
    ) -> int: ...

    def unsubscribe(self, token: int) -> None: ...


class PlaybackSink(Protocol):
    """The outbound side of the bridge: enqueue reply audio and flush."""

    def play_chunk(self, chunk: AudioChunk) -> None: ...

    def interrupt(self) -> None: ...


class ProviderSessionManager:
    """Bridges the Captured Stream to a VoiceProvider and back to playback."""

    def __init__(
        self,
        provider: VoiceProvider,
        source: CapturedSource,
        sink: PlaybackSink,
        on_loss: LossHandler | None = None,
    ) -> None:
        self._provider = provider
        self._source = source
        self._sink = sink
        self._on_loss = on_loss
        self._token: int | None = None
        self._pump: asyncio.Task[None] | None = None
        self._engaged = False

    @property
    def engaged(self) -> bool:
        return self._engaged

    async def engage(self) -> None:
        """Open the session and start forwarding audio (REQ-AIVP-001).

        Raises ProviderError if the session cannot be opened; the pipeline is
        left disengaged in that case (AC-AIVP-001.3).
        """
        if self._engaged:
            raise ProviderError("AI voice pipeline is already engaged.")
        try:
            await self._provider.open()
        except ProviderError:
            raise
        except Exception as exc:  # normalize any provider failure
            raise ProviderError(f"could not open provider session: {exc}") from exc
        # Subscribe only after the session is open so audio is forwarded only
        # while a session exists (AC-AIVP-001.2).
        self._token = self._source.subscribe(self._on_captured, self._provider.input_rate, 1)
        self._pump = asyncio.get_event_loop().create_task(self._drain_replies())
        self._engaged = True
        log.info("AI voice pipeline engaged")

    async def disengage(self) -> None:
        """Stop forwarding and close the session (AC-AIVP-001.4)."""
        if not self._engaged:
            return
        self._engaged = False
        self._detach_source()
        if self._pump is not None:
            self._pump.cancel()
            try:
                await self._pump
            except asyncio.CancelledError:
                pass
            self._pump = None
        try:
            await self._provider.close()
        finally:
            self._sink.interrupt()  # drop any half-played reply on teardown
            log.info("AI voice pipeline disengaged")

    def _detach_source(self) -> None:
        if self._token is not None:
            self._source.unsubscribe(self._token)
            self._token = None

    # --- Captured Stream consumer ---------------------------------------
    async def _on_captured(self, chunk: AudioChunk) -> None:
        """Forward one captured 16 kHz mono chunk to the provider.

        The resampler already delivered mono PCM at the provider input rate
        (AC-AIVP-002.1/.3); we send it straight through in small chunks
        (AC-AIVP-002.2).
        """
        if not self._engaged or not chunk.pcm:
            return
        try:
            await self._provider.send_audio(chunk.pcm)
        except ProviderError:
            await self._handle_loss("provider input stream failed")
        except Exception:
            log.exception("failed to send captured audio to provider")

    # --- reply stream ---------------------------------------------------
    async def _drain_replies(self) -> None:
        """Pump provider reply events into the Playback Sink (REQ-AIVP-003/004)."""
        try:
            async for event in self._provider.events():
                self._handle_event(event)
        except asyncio.CancelledError:
            raise
        except ProviderError:
            await self._handle_loss("provider session lost")
        except Exception:
            log.exception("unexpected error draining provider replies")
            await self._handle_loss("provider reply stream failed")

    def _handle_event(self, event: ReplyEvent) -> None:
        if event.kind is ReplyEventKind.AUDIO:
            if event.pcm:
                # Mono reply PCM at the provider output rate; the Playback
                # Service resampler up-converts to the 48 kHz call boundary
                # (AC-AIVP-003.2 / AC-INJ-002.2).
                self._sink.play_chunk(
                    AudioChunk(pcm=event.pcm, rate=self._provider.output_rate, channels=1)
                )
        elif event.kind is ReplyEventKind.INTERRUPTED:
            # Drop pending reply audio and stop playback at once
            # (REQ-AIVP-004 / REQ-INJ-003).
            self._sink.interrupt()
        elif event.kind is ReplyEventKind.TURN_COMPLETE:
            # Segment boundary; buffer drains to silence on its own
            # (AC-AIVP-003.3 / AC-INJ-002.3). Nothing to flush.
            log.debug("provider turn complete")

    async def _handle_loss(self, reason: str) -> None:
        """Report an unrecoverable loss and disengage (AC-AIVP-005.2)."""
        if not self._engaged:
            return
        log.warning("AI voice pipeline lost: %s", reason)
        self._engaged = False
        self._detach_source()
        try:
            await self._provider.close()
        except Exception:
            log.exception("error closing provider after loss")
        finally:
            self._sink.interrupt()
        if self._on_loss is not None:
            await self._on_loss(reason)
