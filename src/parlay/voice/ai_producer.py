"""AI Voice Pipeline as an Audio Producer behind the Audio Output Arbiter.

The AI pipeline drives the call output through the Arbiter rather than the
Playback Sink directly (REQ-INJ-006). `AiVoiceProducer` acquires the output
when engaged and releases it when disengaged; the `ProviderSessionManager`
receives an Arbiter-backed sink handle so its reply audio is gated by the
Arbiter.

Pause/resume implement the producer contract the Arbiter calls on handover:
pausing drops any half-played reply so the preempting producer's audio does not
mix; resuming is a no-op because the provider stream simply continues once the
AI regains the output.
"""

from __future__ import annotations

import logging

from ..audio.arbiter import AudioOutputArbiter, PlaybackHandle
from .provider import VoiceProvider
from .session_manager import (
    CapturedSource,
    LossHandler,
    ProviderSessionManager,
    SpeakingHandler,
)

log = logging.getLogger(__name__)


class AiVoiceProducer:
    """Owns the AI pipeline and its hold on the call output."""

    def __init__(
        self,
        provider: VoiceProvider,
        source: CapturedSource,
        arbiter: AudioOutputArbiter,
        on_loss: LossHandler | None = None,
        on_speaking: SpeakingHandler | None = None,
    ) -> None:
        self._arbiter = arbiter
        self._sink: PlaybackHandle = arbiter.handle_for(self)
        self._pipeline = ProviderSessionManager(
            provider,
            source=source,
            sink=self._sink,
            on_loss=on_loss,
            on_speaking=on_speaking,
        )

    @property
    def engaged(self) -> bool:
        return self._pipeline.engaged

    async def engage(self) -> None:
        """Take the call output and open the provider session."""
        await self._arbiter.acquire(self)
        try:
            await self._pipeline.engage()
        except Exception:
            # Do not keep holding the output if the session cannot open.
            await self._arbiter.release(self)
            raise

    async def disengage(self) -> None:
        """Close the provider session and release the call output."""
        try:
            await self._pipeline.disengage()
        finally:
            await self._arbiter.release(self)

    # --- AudioProducer contract (called by the Arbiter on handover) -----
    async def pause(self) -> None:
        """Drop pending reply audio so a preempting producer does not mix."""
        self._sink.interrupt()

    async def resume(self) -> None:
        """No-op: the provider stream continues once the AI regains output."""
        log.debug("AI voice producer resumed")
