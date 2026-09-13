"""ProviderSessionManager: drives a VoiceProvider across the bridge.

On engage it opens the provider session, subscribes to the Captured Stream as a
16 kHz mono consumer, forwards each captured chunk upstream, and drains the
provider's reply events into the Playback Sink. On disengage it detaches and
closes the session. Interruptions flush pending playback immediately; an
unrecoverable session loss reports to the Operator and disengages.

Turn shaping (natural feel):
  * Pre-roll priming. Native-audio replies arrive in bursts that can outrun the
    fixed 10 ms playout callback, so the first words underrun the buffer and
    sound shattered. The manager holds the first `_PRIME_MS` of a turn's audio
    and releases it in one enqueue, giving the playout a cushion so the opening
    words are smooth.
  * Inter-turn gap. A short `_GAP_MS` of silence is prepended to each turn so the
    AI does not start speaking on top of the user and the exchange feels paced.

This module is provider-agnostic (talks to `VoiceProvider`) and bridge-agnostic
(talks to the `CapturedSource` / `PlaybackSink` protocols), so it is fully unit
testable with fakes. `RawAudioBridge` satisfies both protocols structurally.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Protocol

from ..audio.frames import AudioChunk
from .provider import ProviderError, ReplyEvent, ReplyEventKind, VoiceProvider

log = logging.getLogger(__name__)

# Called when the pipeline disengages itself after an unrecoverable loss, so the
# app can report to the Operator and reconcile session state. The argument is a
# human-readable reason.
LossHandler = Callable[[str], Awaitable[None]]

# Called when the AI first begins emitting reply audio for a turn, so the app can
# announce in-call that the AI is speaking (REQ-AIVP-008.1).
SpeakingHandler = Callable[[], Awaitable[None]]

# Called (unthrottled) on the first reply audio of every turn and again when the
# turn ends or is interrupted. The app uses these to unmute the userbot while the
# AI speaks and mute it in between, so participants do not hear dead air.
ReplyBoundaryHandler = Callable[[], Awaitable[None]]

# Minimum seconds between AI-speaking announcements within one engaged session,
# so repeated turns do not flood the chat (REQ-AIVP-008.3).
_SPEAKING_THROTTLE_S = 30.0

# Milliseconds of reply audio to buffer before the first chunk of a turn is
# allowed to play, so bursty early frames do not underrun the playout.
_PRIME_MS = 320

# Milliseconds of silence prepended to each turn so replies feel paced and do
# not begin on top of the user.
_GAP_MS = 200


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
        on_speaking: SpeakingHandler | None = None,
        on_reply_start: ReplyBoundaryHandler | None = None,
        on_reply_end: ReplyBoundaryHandler | None = None,
    ) -> None:
        self._provider = provider
        self._source = source
        self._sink = sink
        self._on_loss = on_loss
        self._on_speaking = on_speaking
        self._on_reply_start = on_reply_start
        self._on_reply_end = on_reply_end
        self._token: int | None = None
        self._pump: asyncio.Task[None] | None = None
        self._engaged = False
        # Announcement throttle state (REQ-AIVP-008.1/.3).
        self._turn_announced = False
        self._last_announce: float | None = None
        # Per-turn playout shaping state.
        self._turn_active = False
        self._primed = False
        self._prime_buf = bytearray()
        # Current speaker context already sent to the provider.
        self._speaker: str | None = None

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
        self._turn_announced = False
        self._last_announce = None
        self._reset_turn()
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

    # --- context injection -----------------------------------------------------
    def note_context(self, text: str) -> None:
        """Send arbitrary best-effort context text to the provider.

        Used for roster and speaker awareness: multiple participants share one
        mixed input stream, so the model cannot tell voices apart on its own.
        The provider may not support context text; failures are swallowed and a
        provider without `send_context` is a no-op.
        """
        if not text or not self._engaged:
            return
        send = getattr(self._provider, "send_context", None)
        if send is None:
            return

        async def push() -> None:
            try:
                await send(text)
            except Exception:
                log.debug("failed to send context", exc_info=True)

        asyncio.get_event_loop().create_task(push())

    def note_speaker(self, name: str | None) -> None:
        """Tell the provider who is currently speaking, best-effort."""
        if not name or name == self._speaker:
            return
        self._speaker = name
        self.note_context(f"Abhi {name} bol rahe hain.")

    # --- Captured Stream consumer ----------------------------------------------
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

    # --- reply stream ----------------------------------------------------------
    async def _drain_replies(self) -> None:
        """Pump provider reply events into the Playback Sink (REQ-AIVP-003/004)."""
        try:
            async for event in self._provider.events():
                await self._handle_event(event)
        except asyncio.CancelledError:
            raise
        except ProviderError:
            await self._handle_loss("provider session lost")
        except Exception:
            log.exception("unexpected error draining provider replies")
            await self._handle_loss("provider reply stream failed")

    async def _handle_event(self, event: ReplyEvent) -> None:
        if event.kind is ReplyEventKind.AUDIO:
            if event.pcm:
                await self._on_turn_audio(event.pcm)
        elif event.kind is ReplyEventKind.INTERRUPTED:
            # Drop pending reply audio and stop playback at once
            # (REQ-AIVP-004 / REQ-INJ-003).
            self._sink.interrupt()
            await self._end_turn()
        elif event.kind is ReplyEventKind.TURN_COMPLETE:
            # Segment boundary; buffer drains to silence on its own
            # (AC-AIVP-003.3 / AC-INJ-002.3). Flush any held pre-roll first.
            self._flush_prime()
            log.debug("provider turn complete")
            await self._end_turn()

    async def _on_turn_audio(self, pcm: bytes) -> None:
        """Handle one chunk of reply audio, applying pre-roll and the gap."""
        if not self._turn_active:
            await self._begin_turn()
        await self._announce_speaking()
        if self._primed:
            self._emit(pcm)
            return
        # Still priming: accumulate until we have a cushion, then release.
        self._prime_buf.extend(pcm)
        if len(self._prime_buf) >= self._prime_bytes():
            self._flush_prime()

    async def _begin_turn(self) -> None:
        """Start a new reply turn: unmute, gap of silence, then begin priming."""
        self._turn_active = True
        self._primed = False
        self._prime_buf.clear()
        # Signal reply start first so the app can unmute the outgoing stream
        # before any audio is queued, avoiding a clipped opening.
        if self._on_reply_start is not None:
            try:
                await self._on_reply_start()
            except Exception:
                log.exception("reply-start hook failed")
        gap = self._gap_bytes()
        if gap:
            self._emit(b"\x00" * gap)

    def _flush_prime(self) -> None:
        """Release any held pre-roll audio and switch to pass-through."""
        if self._prime_buf:
            self._emit(bytes(self._prime_buf))
            self._prime_buf.clear()
        self._primed = True

    async def _end_turn(self) -> None:
        """Reset per-turn state and notify the app the AI stopped speaking."""
        was_active = self._turn_active
        self._reset_turn()
        if was_active and self._on_reply_end is not None:
            try:
                await self._on_reply_end()
            except Exception:
                log.exception("reply-end hook failed")

    def _reset_turn(self) -> None:
        self._turn_active = False
        self._primed = False
        self._prime_buf.clear()
        self._turn_announced = False

    def _emit(self, pcm: bytes) -> None:
        """Enqueue reply PCM at the provider output rate for playback.

        The Playback Service resampler up-converts to the 48 kHz call boundary
        (AC-AIVP-003.2 / AC-INJ-002.2).
        """
        self._sink.play_chunk(AudioChunk(pcm=pcm, rate=self._provider.output_rate, channels=1))

    def _prime_bytes(self) -> int:
        return _ms_to_bytes(self._provider.output_rate, _PRIME_MS)

    def _gap_bytes(self) -> int:
        return _ms_to_bytes(self._provider.output_rate, _GAP_MS)

    async def _announce_speaking(self) -> None:
        """Post an in-call 'AI is speaking' note on the first audio of a turn.

        Fires at most once per turn and, across turns, at most once per throttle
        window so repeated turns do not flood the chat (REQ-AIVP-008.1/.3).
        """
        if self._turn_announced:
            return
        self._turn_announced = True
        if self._on_speaking is None:
            return
        now = time.monotonic()
        if self._last_announce is not None and now - self._last_announce < _SPEAKING_THROTTLE_S:
            return
        self._last_announce = now
        try:
            await self._on_speaking()
        except Exception:
            log.exception("failed to post AI-speaking announcement")

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


def _ms_to_bytes(rate: int, ms: int) -> int:
    """Bytes of 16-bit mono PCM for `ms` milliseconds at `rate` Hz."""
    return (rate * 2 * ms) // 1000
